import os
import logging

from firebase_admin import messaging
from django.conf import settings

from notifications.models import Notification

logger = logging.getLogger('notifications')


def fcm_push(notification_id, tokens=None):
    """
    FCM message push, either to all users subscribed to the
    topic(category slug), or to a specific user targeted by the specified token.
    :param notification_id: Notification id.
    :param tokens: Specific tokens of clients to push the notification to.
    :return: None
    """

    try:
        notification = Notification.objects.get(id=notification_id)
        app_config = notification.category.app_config

    except Notification.DoesNotExist:
        logger.exception(f'Notification #{notification_id} does not exist')
        return False

    # FCM only accepts string values in the data map
    payload = {
        key: '' if value is None else str(value)
        for key, value in (notification.data or {}).items()
    }

    base_notification = messaging.Notification(
        title=payload.get('title') or notification.category.name,
        body=notification.template,
    )
    android_conf = messaging.AndroidConfig(
        notification=messaging.AndroidNotification(
            click_action=notification.android_onclick_activity or None
        )
    )
    apns_conf = messaging.APNSConfig(
        payload=messaging.APNSPayload(
            aps=messaging.Aps(sound='default')
        )
    )

    if app_config is not None and app_config.assets is not None:
        icon_url = os.path.join(
            settings.STATIC_URL,
            app_config.base_urls.static,
            'assets',
            app_config.assets.logo,
        )
    else:
        icon_url = os.path.join(
                settings.STATIC_URL,
                'notifications',
                settings.DISCOVERY.get_app_configuration(
                    'notifications'
                ).base_urls.static,
                'bell_icon.svg'
        )

    webpush_conf = messaging.WebpushConfig(
        notification=messaging.WebpushNotification(
            icon=icon_url,
            actions=[
                messaging.WebpushNotificationAction(
                    action=notification.web_onclick_url,
                    title=f'Open {notification.web_onclick_url}'
                )
            ]
        )
    )

    # If tokens are provided, make a multicast message
    # else it is assumed that it is a topic based broadcast
    if tokens:
        message = messaging.MulticastMessage(
            data=payload,
            notification=base_notification,
            android=android_conf,
            apns=apns_conf,
            webpush=webpush_conf,
            tokens=tokens
        )
    else:
        message = messaging.Message(
            data=payload,
            notification=base_notification,
            android=android_conf,
            apns=apns_conf,
            webpush=webpush_conf,
            topic=notification.category.slug
        )

    try:
        # use dry_run = True for testing purpose
        if tokens:
            _ = messaging.send_multicast(message, dry_run=False)
        else:
            _ = messaging.send(message, dry_run=False)
    except Exception:
        logger.exception(f'Could not push notification #{notification_id}')
        return False

    return True


def fcm_subscribe(tokens, topic):
    """
    Subscribe the given tokens to a specific FCM topic.
    :param tokens: list of FCM tokens
    :param topic: FCM topic/category slug
    :return: success True/False
    """

    try:
        response = messaging.subscribe_to_topic(tokens, topic)
    except Exception:
        logger.exception(f'Could not subscribe tokens to the topic {topic}')
        return False

    return _check_topic_response(response, 'subscribe to', topic)


def fcm_unsubscribe(tokens, topic):
    """
    Unsubscribe the given tokens from a specific FCM topic.
    :param tokens: list of FCM tokens
    :param topic: FCM topic/category slug
    :return: success True/False
    :return:
    """

    try:
        response = messaging.unsubscribe_from_topic(tokens, topic)
    except Exception:
        logger.exception(
            f'Could not unsubscribe tokens from the topic {topic}'
        )
        return False

    return _check_topic_response(response, 'unsubscribe from', topic)


def _check_topic_response(response, operation, topic):
    """
    Log the tokens that FCM refused to move, which it reports in the response
    rather than by raising
    :param response: TopicManagementResponse from FCM
    :param operation: Description of the operation, for the log
    :param topic: FCM topic/category slug
    :return: success True/False
    """

    if response.failure_count:
        reasons = ', '.join(error.reason for error in response.errors)
        logger.warning(
            f'{response.failure_count} tokens could not {operation} the topic '
            f'{topic}: {reasons}'
        )
        return False

    return True
