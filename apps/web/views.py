"""web/views.py — serves the single-page dashboard shell."""

from django.views.generic import TemplateView


class DashboardView(TemplateView):
    """The SPA shell. All data is loaded client-side from /api/* (see api.py)."""

    template_name = "dashboard.html"
