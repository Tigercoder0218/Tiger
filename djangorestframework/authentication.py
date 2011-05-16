"""
The :mod:`authentication` module provides a set of pluggable authentication classes.

Authentication behavior is provided by mixing the :class:`mixins.AuthMixin` class into a :class:`View` class.

The set of authentication methods which are used is then specified by setting the
:attr:`authentication` attribute on the :class:`View` class, and listing a set of authentication classes.
"""

from django.contrib.auth import authenticate
from django.middleware.csrf import CsrfViewMiddleware
from djangorestframework.utils import as_tuple
import base64

__all__ = (
    'BaseAuthenticaton',
    'BasicAuthenticaton',
    'UserLoggedInAuthenticaton'
)


class BaseAuthenticaton(object):
    """
    All authentication classes should extend BaseAuthentication.
    
    :param view: :class:`Authentication` classes are always passed the current view on creation.
    """

    def __init__(self, view):
        """
        """
        self.view = view

    def authenticate(self, request):
        """
        :param request: Request to be authenticated
        :rtype: :obj:`User` object or None [*]_

        .. Note::
            This function must be overridden to be implemented.
        
        .. [*] The authentication context *will* typically be a :obj:`User` object,
            but it need not be.  It can be any user-like object so long as the
            permissions classes on the view can handle the object and use
            it to determine if the request has the required permissions or not. 
    
            This can be an important distinction if you're implementing some token
            based authentication mechanism, where the authentication context
            may be more involved than simply mapping to a :obj:`User`.
        """
        return None


class BasicAuthenticaton(BaseAuthenticaton):
    """
    Use HTTP Basic authentication.
    """

    def authenticate(self, request):
        from django.utils.encoding import smart_unicode, DjangoUnicodeDecodeError
        
        if 'HTTP_AUTHORIZATION' in request.META:
            auth = request.META['HTTP_AUTHORIZATION'].split()
            if len(auth) == 2 and auth[0].lower() == "basic":
                try:
                    auth_parts = base64.b64decode(auth[1]).partition(':')
                except TypeError:
                    return None
                
                try:
                    uname, passwd = smart_unicode(auth_parts[0]), smart_unicode(auth_parts[2])
                except DjangoUnicodeDecodeError:
                    return None
                    
                user = authenticate(username=uname, password=passwd)
                if user is not None and user.is_active:
                    return user
        return None
                

class UserLoggedInAuthenticaton(BaseAuthenticaton):
    """
    Use Django's session framework for authentication.
    """

    def authenticate(self, request):
        # TODO: Switch this back to request.POST, and let FormParser/MultiPartParser deal with the consequences.
        if getattr(request, 'user', None) and request.user.is_active:
            # If this is a POST request we enforce CSRF validation.
            if request.method.upper() == 'POST':
                # Temporarily replace request.POST with .DATA,
                # so that we use our more generic request parsing
                request._post = self.view.DATA
                resp = CsrfViewMiddleware().process_view(request, None, (), {})
                del(request._post)
                if resp is not None:  # csrf failed
                    return None
            return request.user
        return None


# TODO: TokenAuthentication, DigestAuthentication, OAuthAuthentication
