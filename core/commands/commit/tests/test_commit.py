from unittest.mock import patch

from django.test import TransactionTestCase

from codecov_auth.tests.factories import OwnerFactory
from core.tests.factories import CommitFactory, PullFactory, RepositoryFactory

from ..commit import CommitCommands


class CommitCommandsTest(TransactionTestCase):
    def setUp(self):
        self.user = OwnerFactory(username="codecov-user")
        self.repository = RepositoryFactory()
        self.commit = CommitFactory()
        self.pull = PullFactory(repository_id=self.repository.repoid)
        self.command = CommitCommands(self.user, "github")

    @patch("core.commands.commit.commit.FetchCommitsInteractor.execute")
    def test_fetch_commits_delegate_to_interactor(self, interactor_mock):
        self.filters = None
        self.command.fetch_commits(self.repository, self.filters)
        interactor_mock.assert_called_once_with(self.repository, self.filters)

    @patch("core.commands.commit.commit.FetchCommitsByPullidInteractor.execute")
    def test_fetch_commits_by_pullid_delegate_to_interactor(self, interactor_mock):
        self.command.fetch_commits_by_pullid(self.pull)
        interactor_mock.assert_called_once_with(self.pull)

    @patch("core.commands.commit.commit.GetUploadsOfCommitInteractor.execute")
    def test_get_uploads_of_commit_delegate_to_interactor(self, interactor_mock):
        commit = CommitFactory()
        self.command.get_uploads_of_commit(commit)
        interactor_mock.assert_called_once_with(commit)

    @patch("core.commands.commit.commit.GetFinalYamlInteractor.execute")
    def test_get_final_yaml_delegate_to_interactor(self, interactor_mock):
        self.command.get_final_yaml(self.commit)
        interactor_mock.assert_called_once_with(self.commit)

    @patch("core.commands.commit.commit.GetFileContentInteractor.execute")
    def test_get_file_content_delegate_to_interactor(self, interactor_mock):
        self.command.get_file_content(self.commit, "path/to/file")
        interactor_mock.assert_called_once_with(self.commit, "path/to/file")

    @patch("core.commands.commit.commit.GetCommitErrorsInteractor.execute")
    def test_get_commit_errors_delegate_to_interactor(self, interactor_mock):
        self.command.get_commit_errors(self.commit, "YAML_ERROR")
        interactor_mock.assert_called_once_with(self.commit, "YAML_ERROR")

    @patch("core.commands.commit.commit.GetUploadsNumberInteractor.execute")
    def test_get_uploads_number_delegate_to_interactor(self, interactor_mock):
        self.command.get_uploads_number(self.commit)
        interactor_mock.assert_called_once_with(self.commit)
