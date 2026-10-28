"""Auth (api.md §2) and Administration (api.md §3) routes."""
from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views
from .customer_clients import CustomerClientViewSet

router = DefaultRouter(trailing_slash=True)
router.register("users", views.UserViewSet, basename="admin-users")
router.register("roles", views.RoleViewSet, basename="admin-roles")
router.register("clients", views.ClientViewSet, basename="admin-clients")
router.register("customer-clients", CustomerClientViewSet, basename="admin-customer-clients")

auth_urlpatterns = [
    path("login/", views.LoginView.as_view(), name="auth-login"),
    path("login/verify-otp/", views.LoginVerifyOtpView.as_view(), name="auth-login-verify-otp"),
    path("login/resend-otp/", views.LoginResendOtpView.as_view(), name="auth-login-resend-otp"),
    path("support/", views.SupportInfoView.as_view(), name="auth-support"),
    path("login-roles/", views.LoginRoleOptionsView.as_view(), name="auth-login-roles"),
    path("login-history/", views.LoginHistoryView.as_view(), name="auth-login-history"),
    path("refresh/", views.RefreshView.as_view(), name="auth-refresh"),
    path("logout/", views.LogoutView.as_view(), name="auth-logout"),
    path("me/", views.MeView.as_view(), name="auth-me"),
    path("change-password/", views.ChangePasswordView.as_view(), name="auth-change-password"),
    path("forgot-password/", views.ForgotPasswordView.as_view(), name="auth-forgot-password"),
    path("verify-reset-otp/", views.VerifyResetOtpView.as_view(), name="auth-verify-reset-otp"),
    path("reset-password/", views.ResetPasswordView.as_view(), name="auth-reset-password"),
    path("sessions/", views.SessionListView.as_view(), name="auth-sessions"),
    path("sessions/revoke-others/", views.SessionRevokeOthersView.as_view(), name="auth-sessions-revoke-others"),
    path("sessions/<uuid:pk>/", views.SessionDetailView.as_view(), name="auth-session-detail"),
]

admin_urlpatterns = [
    path("permissions/", views.PermissionCatalogueView.as_view(), name="admin-permissions"),
    path("", include(router.urls)),
]
