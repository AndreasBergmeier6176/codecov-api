import logging
import re

from django.http import HttpRequest, HttpResponseNotAllowed, HttpResponseNotFound
from rest_framework.generics import ListCreateAPIView
from rest_framework.permissions import AllowAny

log = logging.getLogger(__name__)


class ReportViews(ListCreateAPIView):
    permission_classes = [
        AllowAny,
    ]

    def create(self, request: HttpRequest, repo: str, commit_id: str):
        log.info(
            "Request to create new report", extra=dict(repo=repo, commit_id=commit_id)
        )
        return HttpResponseNotFound("Not available")

    def list(self, request: HttpRequest, repo: str, commit_id: str):
        return HttpResponseNotAllowed(permitted_methods=["POST"])
