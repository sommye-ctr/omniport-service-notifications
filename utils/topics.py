def joins_topic(category):
    """
    Whether a notifications subscription to the category joins its FCM topic.
    A category opts out with "fcmTopic": false in its meta, for rows that are
    only ever pushed to directly, so that their topic never reaches anybody.
    :param category: Category object
    :return: True/False
    """

    return (category.meta or {}).get('fcmTopic') is not False
