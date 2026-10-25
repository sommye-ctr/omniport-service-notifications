"""
Tests for what a push carries and for the tokens it finds dead

Run from the repository folder, in an environment with Django and
firebase_admin installed, with
    python -m unittest discover --start-directory tests
"""

import io
import time
import types
import unittest
from unittest import mock

from firebase_admin import exceptions, messaging

import stubs
from stubs import Category, PushEndpoint, actions, fcm, prune, tasks

DATA = {
    'type': 'results',
    'profileId': '',
    'season': 'pla',
    'title': 'Results · Placement',
    'notificationId': '7',
}


def batch_response(dead):
    """
    Answer a multicast send with the given exception for each token in
    `dead`, and success for the rest, in the order of the message's tokens
    """

    def send_multicast(message, dry_run=False):
        return messaging.BatchResponse([
            messaging.SendResponse(
                None if token in dead else {'name': f'messages/{token}'},
                dead.get(token),
            )
            for token in message.tokens
        ])
    return send_multicast


def unregistered():
    return messaging.UnregisteredError('Requested entity was not found.')


def store(person_id, field, token):
    PushEndpoint(
        person_id=person_id, session_id=field, endpoint=token
    ).save()


class PayloadTestCase(unittest.TestCase):
    """
    The data map and its title reach both kinds of message
    """

    def setUp(self):
        stubs.reset()
        self.category = Category(
            'placement_and_internship__results__pla', name='Results'
        )

    def topic_message(self, data):
        stubs.notification(7, data=data, category=self.category)
        with mock.patch.object(messaging, 'send') as send:
            self.assertTrue(fcm.fcm_push(7))
        return send.call_args.args[0]

    def multicast_message(self, data):
        stubs.notification(7, data=data, category=self.category)
        with mock.patch.object(
                messaging, 'send_multicast', side_effect=batch_response({})
        ) as send_multicast:
            self.assertTrue(fcm.fcm_push(7, tokens=['a', 'b']))
        return send_multicast.call_args.args[0]

    def test_the_topic_message_carries_the_data_map(self):
        message = self.topic_message(DATA)

        self.assertEqual(message.data, DATA)
        self.assertEqual(message.topic, self.category.slug)

    def test_the_multicast_message_carries_the_data_map(self):
        message = self.multicast_message(DATA)

        self.assertEqual(message.data, DATA)
        self.assertEqual(message.tokens, ['a', 'b'])

    def test_the_title_comes_from_the_data_map(self):
        for message in (
                self.topic_message(DATA), self.multicast_message(DATA)
        ):
            self.assertEqual(message.notification.title, DATA['title'])

    def test_the_title_falls_back_to_the_category_name(self):
        message = self.topic_message(None)

        self.assertEqual(message.notification.title, 'Results')
        self.assertEqual(message.data, {})

    def test_every_value_is_sent_as_a_string(self):
        message = self.topic_message({'profileId': 4900, 'season': None})

        self.assertEqual(message.data, {'profileId': '4900', 'season': ''})

    def test_an_empty_click_action_is_not_sent(self):
        message = self.topic_message(DATA)

        self.assertIsNone(message.android.notification.click_action)

    def test_ios_plays_the_default_sound(self):
        message = self.topic_message(DATA)

        self.assertEqual(message.apns.payload.aps.sound, 'default')

    def test_a_failed_send_is_reported(self):
        stubs.notification(7, data=DATA)
        with mock.patch.object(
                messaging, 'send', side_effect=ValueError('Invalid message')
        ):
            self.assertFalse(fcm.fcm_push(7))

    def test_a_multicast_without_tokens_is_not_sent_to_the_topic(self):
        stubs.notification(7, data=DATA)
        with mock.patch.object(messaging, 'send') as send:
            self.assertIsNone(fcm.fcm_push_multicast(7, []))
        send.assert_not_called()


class SaveTestCase(unittest.TestCase):
    """
    A notification with a data map records its own id in it
    """

    @staticmethod
    def notification(data):
        notification = types.SimpleNamespace(id=None, data=data)

        def save(**kwargs):
            notification.id = 5
        notification.save = mock.Mock(side_effect=save)
        return notification

    def test_the_id_is_added_to_the_data_map(self):
        notification = self.notification({'type': 'results'})

        actions._save(notification)

        self.assertEqual(
            notification.data, {'type': 'results', 'notificationId': '5'}
        )
        notification.save.assert_called_with(update_fields=['data'])

    def test_a_notification_without_a_data_map_is_saved_once(self):
        notification = self.notification(None)

        actions._save(notification)

        self.assertIsNone(notification.data)
        notification.save.assert_called_once_with()


class DirectSendTestCase(unittest.TestCase):
    """
    A direct send deletes the tokens that will never receive a push again
    """

    def setUp(self):
        stubs.reset()
        stubs.notification(7, data=DATA)
        store(1, 'placement-app:a', 'dead')
        store(1, 'placement-app:b', 'alive')
        store(2, 'placement-app:c', 'elsewhere')
        store(3, 'placement-app:d', 'untouched')

    def send(self, dead, persons=(1, 2)):
        with mock.patch.object(
                messaging, 'send_multicast', side_effect=batch_response(dead)
        ) as send_multicast:
            tasks.execute_users_set_push(7, list(persons))
        return send_multicast

    def test_only_the_given_people_are_sent_to(self):
        send_multicast = self.send({})

        tokens = [
            token
            for call in send_multicast.call_args_list
            for token in call.args[0].tokens
        ]
        self.assertEqual(sorted(tokens), ['alive', 'dead', 'elsewhere'])

    def test_an_unregistered_token_is_removed(self):
        self.send({'dead': unregistered()})

        self.assertEqual(
            PushEndpoint.fetch_fields(1), {'placement-app:b': 'alive'}
        )
        self.assertIsNone(PushEndpoint.owner('dead'))
        self.assertNotIn('placement-app:a', PushEndpoint.fetch_seen(1))

    def test_a_token_of_another_sender_is_removed(self):
        self.send({
            'elsewhere': messaging.SenderIdMismatchError('Mismatched sender')
        })

        self.assertEqual(PushEndpoint.fetch(2), [])

    def test_a_token_is_kept_on_any_other_error(self):
        self.send({
            'dead': exceptions.InvalidArgumentError('Invalid message'),
            'alive': exceptions.UnavailableError('Try again later'),
        })

        self.assertEqual(sorted(PushEndpoint.fetch(1)), ['alive', 'dead'])

    def test_a_token_replaced_since_the_send_is_kept(self):
        store(1, 'placement-app:a', 'newer')

        with mock.patch.object(
                messaging, 'send_multicast',
                side_effect=batch_response({'dead': unregistered()})
        ):
            tasks.push_to_endpoints(7, [(1, 'placement-app:a', 'dead')])

        self.assertEqual(PushEndpoint.fetch_one(1, 'placement-app:a'), 'newer')

    def test_bare_tokens_from_older_tasks_are_still_sent(self):
        with mock.patch.object(
                messaging, 'send_multicast',
                side_effect=batch_response({'dead': unregistered()})
        ) as send_multicast:
            self.assertTrue(tasks.push_to_endpoints(7, ['dead', 'alive']))

        self.assertEqual(
            send_multicast.call_args.args[0].tokens, ['dead', 'alive']
        )
        self.assertEqual(len(PushEndpoint.fetch(1)), 2)

    def test_endpoints_from_a_queued_task_arrive_as_lists(self):
        # Celery serialises the tuples to JSON, which turns them into lists
        with mock.patch.object(
                messaging, 'send_multicast',
                side_effect=batch_response({'dead': unregistered()})
        ):
            tasks.subset_push(7, [[1, 'placement-app:a', 'dead']])

        self.assertEqual(PushEndpoint.fetch(1), ['alive'])

    def test_a_personal_notification_removes_dead_tokens_too(self):
        notification = types.SimpleNamespace(id=7, data=None)
        notification.save = mock.Mock()
        with mock.patch.object(
                actions, 'Notification', return_value=notification
        ), mock.patch.object(
                messaging, 'send_multicast',
                side_effect=batch_response({'dead': unregistered()})
        ) as send_multicast:
            actions.push_notification(
                template='Body',
                category=Category('placement_and_internship__credits_audit'),
                is_personalised=True,
                person=1,
            )

        self.assertEqual(
            sorted(send_multicast.call_args.args[0].tokens), ['alive', 'dead']
        )
        self.assertEqual(PushEndpoint.fetch(1), ['alive'])


class PruneTestCase(unittest.TestCase):
    """
    The weekly prune deletes stale phone tokens and the tokens FCM rejects
    """

    def setUp(self):
        stubs.reset()
        stubs.Subscriptions.rows = [
            (1, Category(
                'placement_and_internship__results__pla', {'fcmTopic': True}
            ), 'notifications'),
        ]
        store(1, 'placement-app:stale', 'stale')
        store(1, 'placement-app:fresh', 'fresh')
        store(1, 'web-session', 'uninstalled')
        store(2, 'old-web-session', 'web')
        long_ago = int(time.time()) - 91 * 24 * 60 * 60
        stubs.redis.hset(
            f'{PushEndpoint.seen_key_prefix}:1', 'placement-app:stale', long_ago
        )
        stubs.redis.hset(
            f'{PushEndpoint.seen_key_prefix}:2', 'old-web-session', long_ago
        )

    def run_command(self, dry_run):
        stdout = io.StringIO()
        patches = (
            mock.patch.object(
                messaging, 'send_multicast',
                side_effect=batch_response({'uninstalled': unregistered()}),
            ),
            mock.patch.object(
                messaging, 'unsubscribe_from_topic',
                return_value=messaging.TopicManagementResponse(
                    {'results': [{}]}
                ),
            ),
        )
        with patches[0] as send_multicast, patches[1] as unsubscribe:
            prune.Command(stdout=stdout).handle(dry_run=dry_run)
        return stdout.getvalue(), send_multicast, unsubscribe

    def test_a_dry_run_lists_the_tokens_and_deletes_nothing(self):
        output, _, unsubscribe = self.run_command(dry_run=True)

        self.assertIn('placement-app:stale', output)
        self.assertIn('web-session', output)
        self.assertIn('Would delete 1 stale and 1 dead tokens out of 4', output)
        self.assertEqual(len(PushEndpoint.fetch(1)), 3)
        unsubscribe.assert_not_called()

    def test_a_stale_phone_token_leaves_its_topics_and_is_deleted(self):
        _, _, unsubscribe = self.run_command(dry_run=False)

        unsubscribe.assert_called_once_with(
            ['stale'], 'placement_and_internship__results__pla'
        )
        self.assertNotIn('placement-app:stale', PushEndpoint.fetch_fields(1))

    def test_a_token_fcm_rejects_is_deleted(self):
        self.run_command(dry_run=False)

        self.assertEqual(
            PushEndpoint.fetch_fields(1), {'placement-app:fresh': 'fresh'}
        )

    def test_only_phone_tokens_expire_by_age(self):
        self.run_command(dry_run=False)

        self.assertEqual(PushEndpoint.fetch(2), ['web'])

    def test_the_check_is_a_dry_run_send(self):
        _, send_multicast, _ = self.run_command(dry_run=False)

        self.assertTrue(send_multicast.call_args.kwargs['dry_run'])
        self.assertEqual(
            sorted(send_multicast.call_args.args[0].tokens),
            ['fresh', 'uninstalled', 'web'],
        )


if __name__ == '__main__':
    unittest.main()
