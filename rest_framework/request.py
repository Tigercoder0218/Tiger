"""
The Request class is used as a wrapper around the standard request object.

The wrapped request then offers a richer API, in particular :

    - content automatically parsed according to `Content-Type` header,
      and available as `request.data`
    - full support of PUT method, including support for file uploads
    - form overloading of HTTP method, content type and content
"""
from __future__ import unicode_literals

import sys

from django.conf import settings
from django.http import QueryDict
from django.http.multipartparser import parse_header
from django.utils import six
from django.utils.datastructures import MultiValueDict

from rest_framework import HTTP_HEADER_ENCODING, exceptions
from rest_framework.settings import api_settings


def is_form_media_type(media_type):
    """
    Return True if the media type is a valid form media type.
    """
    base_media_type, params = parse_header(media_type.encode(HTTP_HEADER_ENCODING))
    return (base_media_type == 'application/x-www-form-urlencoded' or
            base_media_type == 'multipart/form-data')


class override_method(object):
    """
    A context manager that temporarily overrides the method on a request,
    additionally setting the `view.request` attribute.

    Usage:

        with override_method(view, request, 'POST') as request:
            ... # Do stuff with `view` and `request`
    """
    def __init__(self, view, request, method):
        self.view = view
        self.request = request
        self.method = method
        self.action = getattr(view, 'action', None)

    def __enter__(self):
        self.view.request = clone_request(self.request, self.method)
        # For viewsets we also set the `.action` attribute.
        action_map = getattr(self.view, 'action_map', {})
        self.view.action = action_map.get(self.method.lower())
        return self.view.request

    def __exit__(self, *args, **kwarg):
        self.view.request = self.request
        self.view.action = self.action


class Empty(object):
    """
    Placeholder for unset attributes.
    Cannot use `None`, as that may be a valid value.
    """
    pass


def _hasattr(obj, name):
    return not getattr(obj, name) is Empty


def clone_request(request, method):
    """
    Internal helper method to clone a request, replacing with a different
    HTTP method.  Used for checking permissions against other methods.
    """
    ret = Request(request=request._request,
                  parsers=request.parsers,
                  authenticators=request.authenticators,
                  negotiator=request.negotiator,
                  parser_context=request.parser_context)
    ret._data = request._data
    ret._files = request._files
    ret._full_data = request._full_data
    ret._content_type = request._content_type
    ret._stream = request._stream
    ret._method = method
    if hasattr(request, '_user'):
        ret._user = request._user
    if hasattr(request, '_auth'):
        ret._auth = request._auth
    if hasattr(request, '_authenticator'):
        ret._authenticator = request._authenticator
    if hasattr(request, 'accepted_renderer'):
        ret.accepted_renderer = request.accepted_renderer
    if hasattr(request, 'accepted_media_type'):
        ret.accepted_media_type = request.accepted_media_type
    if hasattr(request, 'version'):
        ret.version = request.version
    if hasattr(request, 'versioning_scheme'):
        ret.versioning_scheme = request.versioning_scheme
    return ret


class ForcedAuthentication(object):
    """
    This authentication class is used if the test client or request factory
    forcibly authenticated the request.
    """

    def __init__(self, force_user, force_token):
        self.force_user = force_user
        self.force_token = force_token

    def authenticate(self, request):
        return (self.force_user, self.force_token)


class Request(object):
    """
    Wrapper allowing to enhance a standard `HttpRequest` instance.

    Kwargs:
        - request(HttpRequest). The original request instance.
        - parsers_classes(list/tuple). The parsers to use for parsing the
          request content.
        - authentication_classes(list/tuple). The authentications used to try
          authenticating the request's user.
    """

    _METHOD_PARAM = api_settings.FORM_METHOD_OVERRIDE
    _CONTENT_PARAM = api_settings.FORM_CONTENT_OVERRIDE
    _CONTENTTYPE_PARAM = api_settings.FORM_CONTENTTYPE_OVERRIDE

    def __init__(self, request, parsers=None, authenticators=None,
                 negotiator=None, parser_context=None):
        self._request = request
        self.parsers = parsers or ()
        self.authenticators = authenticators or ()
        self.negotiator = negotiator or self._default_negotiator()
        self.parser_context = parser_context
        self._data = Empty
        self._files = Empty
        self._full_data = Empty
        self._method = Empty
        self._content_type = Empty
        self._stream = Empty

        if self.parser_context is None:
            self.parser_context = {}
        self.parser_context['request'] = self
        self.parser_context['encoding'] = request.encoding or settings.DEFAULT_CHARSET

        force_user = getattr(request, '_force_auth_user', None)
        force_token = getattr(request, '_force_auth_token', None)
        if (force_user is not None or force_token is not None):
            forced_auth = ForcedAuthentication(force_user, force_token)
            self.authenticators = (forced_auth,)

    def _default_negotiator(self):
        return api_settings.DEFAULT_CONTENT_NEGOTIATION_CLASS()

    @property
    def method(self):
        """
        Returns the HTTP method.

        This allows the `method` to be overridden by using a hidden `form`
        field on a form POST request.
        """
        if not _hasattr(self, '_method'):
            self._load_method_and_content_type()
        return self._method

    @property
    def content_type(self):
        """
        Returns the content type header.

        This should be used instead of `request.META.get('HTTP_CONTENT_TYPE')`,
        as it allows the content type to be overridden by using a hidden form
        field on a form POST request.
        """
        if not _hasattr(self, '_content_type'):
            self._load_method_and_content_type()
        return self._content_type

    @property
    def stream(self):
        """
        Returns an object that may be used to stream the request content.
        """
        if not _hasattr(self, '_stream'):
            self._load_stream()
        return self._stream

    @property
    def query_params(self):
        """
        More semantically correct name for request.GET.
        """
        return self._request.GET

    @property
    def data(self):
        if not _hasattr(self, '_full_data'):
            self._load_data_and_files()
        return self._full_data

    @property
    def user(self):
        """
        Returns the user associated with the current request, as authenticated
        by the authentication classes provided to the request.
        """
        if not hasattr(self, '_user'):
            self._authenticate()
        return self._user

    @user.setter
    def user(self, value):
        """
        Sets the user on the current request. This is necessary to maintain
        compatibility with django.contrib.auth where the user property is
        set in the login and logout functions.

        Note that we also set the user on Django's underlying `HttpRequest`
        instance, ensuring that it is available to any middleware in the stack.
        """
        self._user = value
        self._request.user = value

    @property
    def auth(self):
        """
        Returns any non-user authentication information associated with the
        request, such as an authentication token.
        """
        if not hasattr(self, '_auth'):
            self._authenticate()
        return self._auth

    @auth.setter
    def auth(self, value):
        """
        Sets any non-user authentication information associated with the
        request, such as an authentication token.
        """
        self._auth = value
        self._request.auth = value

    @property
    def successful_authenticator(self):
        """
        Return the instance of the authentication instance class that was used
        to authenticate the request, or `None`.
        """
        if not hasattr(self, '_authenticator'):
            self._authenticate()
        return self._authenticator

    def _load_data_and_files(self):
        """
        Parses the request content into `self.data`.
        """
        if not _hasattr(self, '_content_type'):
            self._load_method_and_content_type()

        if not _hasattr(self, '_data'):
            self._data, self._files = self._parse()
            if self._files:
                self._full_data = self._data.copy()
                self._full_data.update(self._files)
            else:
                self._full_data = self._data

    def _load_method_and_content_type(self):
        """
        Sets the method and content_type, and then check if they've
        been overridden.
        """
        self._content_type = self.META.get('HTTP_CONTENT_TYPE',
                                           self.META.get('CONTENT_TYPE', ''))

        self._perform_form_overloading()

        if not _hasattr(self, '_method'):
            self._method = self._request.method

            # Allow X-HTTP-METHOD-OVERRIDE header
            if 'HTTP_X_HTTP_METHOD_OVERRIDE' in self.META:
                self._method = self.META['HTTP_X_HTTP_METHOD_OVERRIDE'].upper()

    def _load_stream(self):
        """
        Return the content body of the request, as a stream.
        """
        try:
            content_length = int(
                self.META.get(
                    'CONTENT_LENGTH', self.META.get('HTTP_CONTENT_LENGTH')
                )
            )
        except (ValueError, TypeError):
            content_length = 0

        if content_length == 0:
            self._stream = None
        elif hasattr(self._request, 'read'):
            self._stream = self._request
        else:
            self._stream = six.BytesIO(self.raw_post_data)

    def _perform_form_overloading(self):
        """
        If this is a form POST request, then we need to check if the method and
        content/content_type have been overridden by setting them in hidden
        form fields or not.
        """

        USE_FORM_OVERLOADING = (
            self._METHOD_PARAM or
            (self._CONTENT_PARAM and self._CONTENTTYPE_PARAM)
        )

        # We only need to use form overloading on form POST requests.
        if (
            self._request.method != 'POST' or
            not USE_FORM_OVERLOADING or
            not is_form_media_type(self._content_type)
        ):
            return

        # At this point we're committed to parsing the request as form data.
        self._data = self._request.POST
        self._files = self._request.FILES
        self._full_data = self._data.copy()
        self._full_data.update(self._files)

        # Method overloading - change the method and remove the param from the content.
        if (
            self._METHOD_PARAM and
            self._METHOD_PARAM in self._data
        ):
            self._method = self._data[self._METHOD_PARAM].upper()

        # Content overloading - modify the content type, and force re-parse.
        if (
            self._CONTENT_PARAM and
            self._CONTENTTYPE_PARAM and
            self._CONTENT_PARAM in self._data and
            self._CONTENTTYPE_PARAM in self._data
        ):
            self._content_type = self._data[self._CONTENTTYPE_PARAM]
            self._stream = six.BytesIO(self._data[self._CONTENT_PARAM].encode(self.parser_context['encoding']))
            self._data, self._files, self._full_data = (Empty, Empty, Empty)

    def _parse(self):
        """
        Parse the request content, returning a two-tuple of (data, files)

        May raise an `UnsupportedMediaType`, or `ParseError` exception.
        """
        stream = self.stream
        media_type = self.content_type

        if stream is None or media_type is None:
            empty_data = QueryDict('', encoding=self._request._encoding)
            empty_files = MultiValueDict()
            return (empty_data, empty_files)

        parser = self.negotiator.select_parser(self, self.parsers)

        if not parser:
            raise exceptions.UnsupportedMediaType(media_type)

        try:
            parsed = parser.parse(stream, media_type, self.parser_context)
        except:
            # If we get an exception during parsing, fill in empty data and
            # re-raise.  Ensures we don't simply repeat the error when
            # attempting to render the browsable renderer response, or when
            # logging the request or similar.
            self._data = QueryDict('', encoding=self._request._encoding)
            self._files = MultiValueDict()
            self._full_data = self._data
            raise

        # Parser classes may return the raw data, or a
        # DataAndFiles object.  Unpack the result as required.
        try:
            return (parsed.data, parsed.files)
        except AttributeError:
            empty_files = MultiValueDict()
            return (parsed, empty_files)

    def _authenticate(self):
        """
        Attempt to authenticate the request using each authentication instance
        in turn.
        Returns a three-tuple of (authenticator, user, authtoken).
        """
        for authenticator in self.authenticators:
            try:
                user_auth_tuple = authenticator.authenticate(self)
            except exceptions.APIException:
                self._not_authenticated()
                raise

            if user_auth_tuple is not None:
                self._authenticator = authenticator
                self.user, self.auth = user_auth_tuple
                return

        self._not_authenticated()

    def _not_authenticated(self):
        """
        Return a three-tuple of (authenticator, user, authtoken), representing
        an unauthenticated request.

        By default this will be (None, AnonymousUser, None).
        """
        self._authenticator = None

        if api_settings.UNAUTHENTICATED_USER:
            self.user = api_settings.UNAUTHENTICATED_USER()
        else:
            self.user = None

        if api_settings.UNAUTHENTICATED_TOKEN:
            self.auth = api_settings.UNAUTHENTICATED_TOKEN()
        else:
            self.auth = None

    def __getattribute__(self, attr):
        """
        If an attribute does not exist on this instance, then we also attempt
        to proxy it to the underlying HttpRequest object.
        """
        try:
            return super(Request, self).__getattribute__(attr)
        except AttributeError:
            info = sys.exc_info()
            try:
                return getattr(self._request, attr)
            except AttributeError:
                six.reraise(info[0], info[1], info[2].tb_next)

    @property
    def DATA(self):
        raise NotImplementedError(
            '`request.DATA` has been deprecated in favor of `request.data` '
            'since version 3.0, and has been fully removed as of version 3.2.'
        )

    @property
    def FILES(self):
        # Leave this one alone for backwards compat with Django's request.FILES
        # Different from the other two cases, which are not valid property
        # names on the WSGIRequest class.
        if not _hasattr(self, '_files'):
            self._load_data_and_files()
        return self._files

    @property
    def QUERY_PARAMS(self):
        raise NotImplementedError(
            '`request.QUERY_PARAMS` has been deprecated in favor of `request.query_params` '
            'since version 3.0, and has been fully removed as of version 3.2.'
        )
