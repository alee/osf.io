# -*- coding: utf-8 -*-
import collections
import httplib as http

from flask import request
from modularodm import Q

from framework.exceptions import HTTPError
from framework.auth.decorators import must_be_logged_in
from framework.auth.utils import privacy_info_handle
from framework.forms.utils import sanitize

from website import settings
from website.filters import gravatar
from website.models import Guid, Comment
from website.project.decorators import must_be_contributor_or_public
from datetime import datetime
from website.project.model import has_anonymous_link
from website.project.views.node import _view_project

@must_be_contributor_or_public
def view_comments(**kwargs):
    """

    """
    node = kwargs['node'] or kwargs['project']
    auth = kwargs['auth']

    serialized = _view_project(node, auth, primary=True)
    from website.addons.wiki.views import _get_wiki_pages_current
    serialized.update({
        'wiki_pages_current': _get_wiki_pages_current(node),
        'wiki_home_content': node.get_wiki_page('home')
    })
    if kwargs.get('cid'):
        comment = kwargs_to_comment(kwargs)
        serialized_comment = serialize_comment(comment, auth)
        serialized.update({
            'comment': serialized_comment,
            'comment_target': serialized_comment['page'],
            'comment_target_id': serialized['node']['id']
        })
    elif kwargs.get('wname'):
        wiki_page = node.get_wiki_page(kwargs.get('wname'))
        if wiki_page is None:
            raise HTTPError(http.NOT_FOUND)
        serialized.update({
            'comment_target': 'wiki',
            'comment_target_id': wiki_page.page_name
        })
    else:
        serialized.update({
            'comment_target': 'node',
            'comment_target_id': serialized['node']['id']
        })
    return serialized


def resolve_target(node, page, guid):
    if not guid:
        return node
    target = Guid.load(guid)
    if target is None:
        if page == 'wiki':
            return node.get_wiki_page(guid, 1)
        raise HTTPError(http.BAD_REQUEST)
    return target.referent


def collect_discussion(target, users=None):

    users = users or collections.defaultdict(list)
    if not getattr(target, 'commented', None) is None:
        for comment in getattr(target, 'commented', []):
            if not comment.is_deleted:
                users[comment.user].append(comment)
            collect_discussion(comment, users=users)
    return users

@must_be_contributor_or_public
def comment_discussion(**kwargs):

    node = kwargs['node'] or kwargs['project']
    auth = kwargs['auth']

    page = request.args.get('page')
    guid = request.args.get('target')

    if page == 'total':
        users = collections.defaultdict(list)
        for comment in getattr(node, 'comment_owner', []) or []:
            if not comment.is_deleted:
                users[comment.user].append(comment)
            collect_discussion(comment, users=users)
    else:
        target = resolve_target(node, page, guid)
        users = collect_discussion(target)
    anonymous = has_anonymous_link(node, auth)

    sorted_users_frequency = sorted(
        users.keys(),
        key=lambda item: len(users[item]),
        reverse=True,
    )

    def get_recency(item):
        most_recent = users[item][0].date_created
        for comment in users[item][1:]:
            if comment.date_created > most_recent:
                most_recent = comment.date_created
        return most_recent

    sorted_users_recency = sorted(
        users.keys(),
        key=lambda item: get_recency(item),
        reverse=True,
    )

    return {
        'discussion_by_frequency': [
            serialize_discussion(node, user, anonymous)
            for user in sorted_users_frequency
        ],
        'discussion_by_recency': [
            serialize_discussion(node, user, anonymous)
            for user in sorted_users_recency
        ]
    }

def serialize_discussion(node, user, anonymous=False):
    return {
        'id': privacy_info_handle(user._id, anonymous),
        'url': privacy_info_handle(user.url, anonymous),
        'fullname': privacy_info_handle(user.fullname, anonymous, name=True),
        'isContributor': node.is_contributor(user),
        'gravatarUrl': privacy_info_handle(
            gravatar(
                user, use_ssl=True, size=settings.GRAVATAR_SIZE_DISCUSSION,
            ),
            anonymous
        )
    }

def serialize_comment(comment, auth, anonymous=False):
    return {
        'id': comment._id,
        'author': {
            'id': privacy_info_handle(comment.user._id, anonymous),
            'url': privacy_info_handle(comment.user.url, anonymous),
            'name': privacy_info_handle(
                comment.user.fullname, anonymous, name=True
            ),
            'gravatarUrl': privacy_info_handle(
                gravatar(
                    comment.user, use_ssl=True,
                    size=settings.GRAVATAR_SIZE_DISCUSSION
                ),
                anonymous
            ),
        },
        'dateCreated': comment.date_created.isoformat(),
        'dateModified': comment.date_modified.isoformat(),
        'page': comment.page,
        'targetId': getattr(comment.target, 'page_name', comment.target._id),
        'rootId': comment.rootId or comment.node._id,
        'content': comment.content,
        'hasChildren': bool(getattr(comment, 'commented', [])),
        'canEdit': comment.user == auth.user,
        'modified': comment.modified,
        'isDeleted': comment.is_deleted,
        'isHidden': comment.is_hidden,
        'isAbuse': auth.user and auth.user._id in comment.reports,
    }


def serialize_comments(record, auth, anonymous=False):

    return [
        serialize_comment(comment, auth, anonymous)
        for comment in getattr(record, 'commented', [])
    ]


def kwargs_to_comment(kwargs, owner=False):

    comment = Comment.load(kwargs.get('cid'))
    if comment is None:
        raise HTTPError(http.BAD_REQUEST)

    if owner:
        auth = kwargs['auth']
        if auth.user != comment.user:
            raise HTTPError(http.FORBIDDEN)

    return comment


@must_be_logged_in
@must_be_contributor_or_public
def add_comment(**kwargs):

    auth = kwargs['auth']
    node = kwargs['node'] or kwargs['project']

    if not node.comment_level:
        raise HTTPError(http.BAD_REQUEST)

    if not node.can_comment(auth):
        raise HTTPError(http.FORBIDDEN)
    page = request.json.get('page')
    guid = request.json.get('target')
    target = resolve_target(node, page, guid)

    content = request.json.get('content').strip()
    content = sanitize(content)
    if not content:
        raise HTTPError(http.BAD_REQUEST)
    if len(content) > settings.COMMENT_MAXLENGTH:
        raise HTTPError(http.BAD_REQUEST)

    comment = Comment.create(
        auth=auth,
        node=node,
        target=target,
        user=auth.user,
        page=page,
        content=content,
    )
    comment.save()

    return {
        'comment': serialize_comment(comment, auth)
    }, http.CREATED


@must_be_contributor_or_public
def list_comments(auth, **kwargs):
    node = kwargs['node'] or kwargs['project']
    anonymous = has_anonymous_link(node, auth)
    page = request.args.get('page')
    guid = request.args.get('target')
    #end = request.args.get('loaded')
    #start = max(0, request.args.get('loaded') - request.args.get('size'))
    if page == 'total':
        serialized_comments = [
            serialize_comment(comment, auth, anonymous)
            for comment in getattr(node, 'comment_owner', [])
        ]
    else:
        target = resolve_target(node, page, guid)
        serialized_comments = serialize_comments(target, auth, anonymous)
    n_unread = 0

    if node.is_contributor(auth.user):
        if auth.user.comments_viewed_timestamp is None:
            auth.user.comments_viewed_timestamp = {}
            auth.user.save()
        n_unread = n_unread_comments(node, auth.user)
    return {
        'comments': serialized_comments,
        'nUnread': n_unread
    }


def n_unread_comments(node, user):
    """Return the number of unread comments on a node for a user."""
    default_timestamp = datetime(1970, 1, 1, 12, 0, 0)
    view_timestamp = user.comments_viewed_timestamp.get(node._id, default_timestamp)
    return Comment.find(Q('node', 'eq', node) &
                        Q('user', 'ne', user) &
                        Q('date_created', 'gt', view_timestamp) &
                        Q('date_modified', 'gt', view_timestamp)).count()

@must_be_logged_in
@must_be_contributor_or_public
def edit_comment(**kwargs):

    auth = kwargs['auth']

    comment = kwargs_to_comment(kwargs, owner=True)

    content = request.json.get('content').strip()
    content = sanitize(content)
    if not content:
        raise HTTPError(http.BAD_REQUEST)
    if len(content) > settings.COMMENT_MAXLENGTH:
        raise HTTPError(http.BAD_REQUEST)

    comment.edit(
        content=content,
        auth=auth,
        save=True
    )

    return serialize_comment(comment, auth)


@must_be_logged_in
@must_be_contributor_or_public
def delete_comment(**kwargs):

    auth = kwargs['auth']
    comment = kwargs_to_comment(kwargs, owner=True)
    comment.delete(auth=auth, save=True)

    return {}


@must_be_logged_in
@must_be_contributor_or_public
def undelete_comment(**kwargs):

    auth = kwargs['auth']
    comment = kwargs_to_comment(kwargs, owner=True)
    comment.undelete(auth=auth, save=True)

    return {}


@must_be_logged_in
@must_be_contributor_or_public
def update_comments_timestamp(auth, **kwargs): # TODO update timestamp for each comment pane, not just overview
    node = kwargs['node'] or kwargs['project']
    if node.is_contributor(auth.user):
        auth.user.comments_viewed_timestamp[node._id] = datetime.utcnow()
        auth.user.save()
        #page = request.json.get('page')
        #list_comments(page=page, **kwargs)
        return {node._id: auth.user.comments_viewed_timestamp[node._id].isoformat()}
    else:
        return {}


@must_be_logged_in
@must_be_contributor_or_public
def report_abuse(**kwargs):

    auth = kwargs['auth']
    user = auth.user

    comment = kwargs_to_comment(kwargs)

    category = request.json.get('category')
    text = request.json.get('text', '')
    if not category:
        raise HTTPError(http.BAD_REQUEST)

    try:
        comment.report_abuse(user, save=True, category=category, text=text)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    return {}


@must_be_logged_in
@must_be_contributor_or_public
def unreport_abuse(**kwargs):

    auth = kwargs['auth']
    user = auth.user

    comment = kwargs_to_comment(kwargs)

    try:
        comment.unreport_abuse(user, save=True)
    except ValueError:
        raise HTTPError(http.BAD_REQUEST)

    return {}
