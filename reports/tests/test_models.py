import json
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase
from shared.storage.exceptions import FileNotInStorageError

from reports.models import ReportDetails
from reports.tests.factories import (
    ReportDetailsFactory,
    RepositoryFlagFactory,
    UploadFactory,
    UploadFlagMembershipFactory,
)


class UploadTests(TestCase):
    def test_get_download_url(self):
        storage_path = "v4/123/123.txt"
        session = UploadFactory(storage_path=storage_path)
        repository = session.report.commit.repository
        assert (
            session.download_url
            == f"/upload/gh/{repository.author.username}/{repository.name}/download?path={storage_path}"
        )

    def test_ci_url_when_no_provider(self):
        session = UploadFactory(provider=None)
        assert session.ci_url is None

    def test_ci_url_when_provider_do_not_have_build_url(self):
        session = UploadFactory(provider="azure_pipelines")
        assert session.ci_url is None

    def test_ci_url_when_provider_has_build_url(self):
        session = UploadFactory(provider="travis", job_code="123")
        repo = session.report.commit.repository
        assert (
            session.ci_url
            == f"https://travis-ci.com/{repo.author.username}/{repo.name}/jobs/{session.job_code}"
        )

    def test_ci_url_when_db_has_build_url(self):
        session = UploadFactory(build_url="http://example.com")
        assert session.ci_url == "http://example.com"

    def test_flags(self):
        session = UploadFactory()
        flag_one = RepositoryFlagFactory()
        flag_two = RepositoryFlagFactory()
        # connect the flag and membership
        UploadFlagMembershipFactory(flag=flag_one, report_session=session)
        UploadFlagMembershipFactory(flag=flag_two, report_session=session)

        assert (
            session.flag_names.sort() == [flag_one.flag_name, flag_two.flag_name].sort()
        )


class TestReportDetails(TestCase):
    sample_files_array = [
        {
            "filename": "test_file_1.py",
            "file_index": 2,
            "file_totals": [1, 10, 8, 2, 5, "80.00000", 6, 7, 9, 8, 20, 40, 13],
            "session_totals": [[0, 10, 8, 2, 0, "80.00000", 0, 0, 0, 0, 0, 0, 0]],
            "diff_totals": [0, 2, 1, 1, 0, "50.00000", 0, 0, 0, 0, 0, 0, 0],
        },
        {
            "filename": "test_file_2.py",
            "file_index": 0,
            "file_totals": [1, 3, 2, 1, 0, "66.66667", 0, 0, 0, 0, 0, 0, 0],
            "session_totals": [[0, 3, 2, 1, 0, "66.66667", 0, 0, 0, 0, 0, 0, 0]],
            "diff_totals": None,
        },
    ]

    @patch("utils.model_utils.ArchiveService")
    def test_get_files_array_from_db(self, mock_archive):
        details = ReportDetailsFactory()
        mock_read_file = MagicMock()
        mock_archive.return_value.read_file = mock_read_file
        details._files_array = self.sample_files_array
        details._files_array_storage_path = None
        details.save()

        fetched = ReportDetails.objects.get(id=details.id)
        assert fetched.files_array == self.sample_files_array
        mock_archive.assert_not_called()
        mock_read_file.assert_not_called()

    @patch("utils.model_utils.ArchiveService")
    def test_get_files_array_from_storage(self, mock_archive):
        details = ReportDetailsFactory()
        storage_path = "https://storage/path/files_array.json"
        mock_read_file = MagicMock(return_value=json.dumps(self.sample_files_array))
        mock_archive.return_value.read_file = mock_read_file
        details._files_array = None
        details._files_array_storage_path = storage_path
        details.save()

        fetched = ReportDetails.objects.get(id=details.id)
        assert fetched.files_array == self.sample_files_array
        mock_archive.assert_called()
        mock_read_file.assert_called_with(storage_path)
        # Calls it again to test caching
        assert fetched.files_array == self.sample_files_array
        assert mock_archive.call_count == 1
        assert mock_read_file.call_count == 1
        # This one to help us understand caching across different instances
        # of the same object. We see that cache is propagated across
        # different instances if they are the same
        assert details.files_array == self.sample_files_array
        assert mock_archive.call_count == 1
        assert mock_read_file.call_count == 1
        # Let's see for objects with different IDs
        diff_details = ReportDetailsFactory()
        storage_path = "https://storage/path/files_array.json"
        diff_details._files_array = None
        diff_details._files_array_storage_path = storage_path
        diff_details.save()
        assert diff_details.files_array == self.sample_files_array
        assert mock_archive.call_count == 2
        assert mock_read_file.call_count == 2

    @patch("utils.model_utils.ArchiveService")
    def test_get_files_array_from_storage_file_not_found(self, mock_archive):
        details = ReportDetailsFactory()
        storage_path = "https://storage/path/files_array.json"

        def side_effect(*args, **kwargs):
            raise FileNotInStorageError()

        mock_read_file = MagicMock(side_effect=side_effect)
        mock_archive.return_value.read_file = mock_read_file
        details._files_array = None
        details._files_array_storage_path = storage_path
        details.save()

        fetched = ReportDetails.objects.get(id=details.id)
        assert fetched.files_array == []
        mock_archive.assert_called()
        mock_read_file.assert_called_with(storage_path)

    @patch("reports.models.get_config")
    def test_write_filed_array_and_retrieve_from_db(self, mock_get_config):
        config = {
            "setup": {
                "save_report_data_in_storage": {
                    "report_details_files_array": False,
                }
            }
        }

        def fake_get_config(*path, default=None):
            curr = config
            for key in path:
                try:
                    curr = curr[key]
                except KeyError:
                    return default
            return curr

        mock_get_config.side_effect = fake_get_config
        details = ReportDetailsFactory()
        assert details.should_write_to_storage() == False
        details.files_array = self.sample_files_array
        details.save()

        fetched = ReportDetails.objects.get(id=details.id)
        assert fetched._files_array == self.sample_files_array
        assert fetched.files_array == self.sample_files_array


@pytest.mark.parametrize(
    "config,owner_username,expected",
    [
        (
            {
                "setup": {
                    "save_report_data_in_storage": {
                        "report_details_files_array": True,
                        "only_codecov": True,
                    }
                }
            },
            "some_owner",
            False,
        ),
        (
            {
                "setup": {
                    "save_report_data_in_storage": {
                        "report_details_files_array": True,
                        "only_codecov": True,
                    }
                }
            },
            "codecov",
            True,
        ),
        (
            {
                "setup": {
                    "save_report_data_in_storage": {
                        "report_details_files_array": True,
                        "only_codecov": False,
                    }
                }
            },
            "some_owner",
            True,
        ),
    ],
)
def test_should_write_to_storage(config, owner_username, expected, mocker, db):
    def fake_get_config(*path, default=None):
        curr = config
        for key in path:
            try:
                curr = curr[key]
            except KeyError:
                return default
        return curr

    mock_get_config = mocker.patch(
        "reports.models.get_config", side_effect=fake_get_config
    )
    report_details = ReportDetailsFactory(
        report__commit__repository__author__username=owner_username
    )
    assert report_details.should_write_to_storage() == expected
