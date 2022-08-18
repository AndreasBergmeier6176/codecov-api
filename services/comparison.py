import asyncio
import copy
import functools
import json
import logging
from collections import Counter
from dataclasses import dataclass

import minio
from asgiref.sync import async_to_sync
from django.utils.functional import cached_property
from shared.helpers.yaml import walk
from shared.reports.types import ReportTotals
from shared.utils.merge import LineType, line_type

from compare.models import CommitComparison
from core.models import Commit
from services import ServiceException
from services.archive import ArchiveService, ReportService
from services.redis_configuration import get_redis_connection
from services.repo_providers import RepoProviderService
from services.task import TaskService
from utils.config import get_config

log = logging.getLogger(__name__)


redis = get_redis_connection()


MAX_DIFF_SIZE = 170


def _is_added(line_value):
    return line_value and line_value[0] == "+"


def _is_removed(line_value):
    return line_value and line_value[0] == "-"


class ComparisonException(ServiceException):
    @property
    def message(self):
        return str(self)


class MissingComparisonCommit(ComparisonException):
    pass


class MissingComparisonReport(ComparisonException):
    pass


class FileComparisonTraverseManager:
    """
    The FileComparisonTraverseManager uses the visitor-pattern to execute a series
    of arbitrary actions on each line in a FileComparison. The main entrypoint to
    this class is the '.apply()' method, which is the only method client code should invoke.
    """

    def __init__(self, head_file_eof=0, base_file_eof=0, segments=[], src=[]):
        """
        head_file_eof -- end-line of the head_file we are traversing, plus 1
        base_file_eof -- same as above, for base_file

        ^^ Generally client code should supply both, except in a couple cases:
          1. The file is newly tracked. In this case, there is no base file, so we should
             iterate only over the head file lines.
          2. The file is deleted. As of right now (4/2/2020), we don't show deleted files in
             comparisons, but if we were to support that, we would not supply a head_file_eof
             and instead only iterate over lines in the base file.

        segments -- these come from the provider API response related to the comparison, and
            constitute the 'diff' between the base and head references. Each segment takes this form:

            {
                "header": [
                    base reference offset,
                    number of lines in file-segment before changes applied,
                    head reference offset,
                    number of lines in file-segment after changes applied
                ],
                "lines": [ # line values for lines in the diff
                  "+this is an added line",
                  "-this is a removed line",
                  "this line is unchanged in the diff",
                  ...
                ]
            }

            The segment["header"], also known as the hunk-header (https://en.wikipedia.org/wiki/Diff#Unified_format),
            is an array of strings, which is why we have to use the int() builtin function
            to compare with self.head_ln and self.base_ln. It is used by this algorithm to
              1. Set initial values for the self.base_ln and self.head_ln line-counters, and
              2. Detect if self.base and/or self.head refer to lines in the diff at any given time

            This algorithm relies on the fact that segments are returned in ascending
            order for each file, which means that the "nearest" segment to the current line
            being traversed is located at segments[0].

        src -- this is the source code of the file at the head-reference, where each line
            is a cell in the array. If we are not traversing a segment, and src is provided,
            the line value passed to the visitors will be the line at src[self.head_ln - 1].
        """
        self.head_file_eof = head_file_eof
        self.base_file_eof = base_file_eof
        self.segments = copy.deepcopy(segments)
        self.src = src

        if self.segments:
            # Base offsets can be 0 if files are added or removed
            self.base_ln = min(1, int(self.segments[0]["header"][0]))
            self.head_ln = min(1, int(self.segments[0]["header"][2]))
        else:
            self.base_ln, self.head_ln = 1, 1

    def traverse_finished(self):
        if self.segments:
            return False
        if self.src:
            return self.head_ln > len(self.src)
        return self.head_ln >= self.head_file_eof and self.base_ln >= self.base_file_eof

    def traversing_diff(self):
        if self.segments == []:
            return False

        base_ln_within_offset = (
            int(self.segments[0]["header"][0])
            <= self.base_ln
            < int(self.segments[0]["header"][0])
            + int(self.segments[0]["header"][1] or 1)
        )
        head_ln_within_offset = (
            int(self.segments[0]["header"][2])
            <= self.head_ln
            < int(self.segments[0]["header"][2])
            + int(self.segments[0]["header"][3] or 1)
        )
        return base_ln_within_offset or head_ln_within_offset

    def pop_line(self):
        if self.traversing_diff():
            return self.segments[0]["lines"].pop(0)

        if self.src:
            return self.src[self.head_ln - 1]

    def apply(self, visitors):
        """
        Traverses the lines in a file comparison while accounting for the diff.
        If a line only appears in the base file (removed in head), it is prefixed
        with '-', and we only increment self.base_ln. If a line only appears in
        the head file, it is newly added and prefixed with '+', and we only
        increment self.head_ln.

        visitors -- A list of visitors applied to each line.
        """
        while not self.traverse_finished():
            line_value = self.pop_line()

            for visitor in visitors:
                visitor(
                    None if _is_added(line_value) else self.base_ln,
                    None if _is_removed(line_value) else self.head_ln,
                    line_value,
                    self.traversing_diff(),  # TODO(pierce): remove when upon combining diff + changes tabs in UI
                )

            if _is_added(line_value):
                self.head_ln += 1
            elif _is_removed(line_value):
                self.base_ln += 1
            else:
                self.head_ln += 1
                self.base_ln += 1

            if self.segments and not self.segments[0]["lines"]:
                # Either the segment has no lines (and is therefore of no use)
                # or all lines have been popped and visited, which means we are
                # done traversing it
                self.segments.pop(0)


class FileComparisonVisitor:
    """
    Abstract class with a convenience method for getting lines amongst
    all the edge cases.
    """

    def _get_line(self, report_file, ln):
        """
        Kindof a hacky way to bypass the dataclasses used in `reports`
        library, because they are extremely slow. This basically copies
        some logic from ReportFile.get and ReportFile._line, which work
        together to take an index and turn it into a ReportLine. Here
        we do something similar, but just return the underlying array instead.
        Not sure if this will be the final solution.

        Note: the underlying array representation cn be seen here:
        https://github.com/codecov/shared/blob/master/shared/reports/types.py#L75
        The index in the array representation is 1-1 with the index of the
        dataclass attribute for ReportLine.
        """
        if report_file is None or ln is None:
            return None

        # copied from ReportFile.get
        try:
            line = report_file._lines[ln - 1]
        except IndexError:
            return None

        # copied from ReportFile._line, minus dataclass instantiation
        if line:
            if type(line) is list:
                return line
            else:
                # these are old versions
                # note:(pierce) ^^ this comment is copied, not sure what it means
                return json.loads(line)

    def _get_lines(self, base_ln, head_ln):
        base_line = self._get_line(self.base_file, base_ln)
        head_line = self._get_line(self.head_file, head_ln)
        return base_line, head_line

    def __call__(self, base_ln, head_ln, value, is_diff):
        pass


class CreateLineComparisonVisitor(FileComparisonVisitor):
    """
    A visitor that creates LineComparisons, and stores the
    result in self.lines. Only operates on lines that have
    code-values derived from segments or src in FileComparisonTraverseManager.
    """

    def __init__(self, base_file, head_file):
        self.base_file, self.head_file = base_file, head_file
        self.lines = []

    def __call__(self, base_ln, head_ln, value, is_diff):
        if value is None:
            return

        base_line, head_line = self._get_lines(base_ln, head_ln)

        self.lines.append(
            LineComparison(
                base_line=base_line,
                head_line=head_line,
                base_ln=base_ln,
                head_ln=head_ln,
                value=value,
                is_diff=is_diff,
            )
        )


class CreateChangeSummaryVisitor(FileComparisonVisitor):
    """
    A visitor for summarizing the "unexpected coverage changes"
    to a certain file. We specifically ignore lines that are changed
    in the source code, which are prefixed with '+' or '-'. Result
    is stored in self.summary.
    """

    def __init__(self, base_file, head_file):
        self.base_file, self.head_file = base_file, head_file
        self.summary = Counter()
        self.coverage_type_map = {
            LineType.hit: "hits",
            LineType.miss: "misses",
            LineType.partial: "partials",
        }

    def _update_summary(self, base_line, head_line):
        """
        Updates the change summary based on the coverage type (0
        for miss, 1 for hit, 2 for partial) found at index 0 of the
        line-array.
        """
        self.summary[self.coverage_type_map[line_type(base_line[0])]] -= 1
        self.summary[self.coverage_type_map[line_type(head_line[0])]] += 1

    def __call__(self, base_ln, head_ln, value, is_diff):
        if value and value[0] in ["+", "-"]:
            return

        base_line, head_line = self._get_lines(base_ln, head_ln)
        if base_line is None or head_line is None:
            return

        if line_type(base_line[0]) == line_type(head_line[0]):
            return

        self._update_summary(base_line, head_line)


class LineComparison:
    def __init__(self, base_line, head_line, base_ln, head_ln, value, is_diff):
        self.base_line = base_line
        self.head_line = head_line
        self.head_ln = head_ln
        self.base_ln = base_ln
        self.value = value
        self.is_diff = is_diff

        self.added = _is_added(value)
        self.removed = _is_removed(value)

    @property
    def number(self):
        return {
            "base": self.base_ln if not self.added else None,
            "head": self.head_ln if not self.removed else None,
        }

    @property
    def coverage(self):
        return {
            "base": None
            if self.added or not self.base_line
            else line_type(self.base_line[0]),
            "head": None
            if self.removed or not self.head_line
            else line_type(self.head_line[0]),
        }

    @property
    def sessions(self):
        """
        Returns the number of LineSessions in the head ReportLine such that
        LineSession.coverage == 1 (indicating a hit).
        """
        if self.head_line is None:
            return None

        # an array of 1's (like [1, 1, ...]) of length equal to the number of sessions
        # where each session's coverage == 1 (hit)
        session_coverage = [
            session[1] for session in self.head_line[2] if session[1] == 1
        ]
        if session_coverage:
            return functools.reduce(lambda a, b: a + b, session_coverage)


class Segment:
    """
    A segment represents a continuous subset set of lines in a file where either
    the coverage has changed or the code has changed (i.e. is part of a diff).
    """

    # additional lines included before and after each segment
    padding_lines = 3

    # max distance between lines with coverage changes in a single segment
    line_distance = 6

    @classmethod
    def segments(cls, file_comparison):
        lines = file_comparison.lines

        # line numbers of interest (i.e. coverage changed or code changed)
        line_numbers = []
        for idx, line in enumerate(lines):
            if (
                line.coverage["base"] != line.coverage["head"]
                or line.added
                or line.removed
            ):
                line_numbers.append(idx)

        segmented_lines = []
        if len(line_numbers) > 0:
            segmented_lines, last = [[]], None
            for line_number in line_numbers:
                if last is None or line_number - last <= cls.line_distance:
                    segmented_lines[-1].append(line_number)
                else:
                    segmented_lines.append([line_number])
                last = line_number

        segments = []
        for group in segmented_lines:
            # padding lines before first line of interest
            start_line_number = group[0] - cls.padding_lines
            start_line_number = max(start_line_number, 0)
            # padding lines after last line of interest
            end_line_number = group[-1] + cls.padding_lines
            end_line_number = min(end_line_number, len(lines) - 1)

            segment = cls(lines[start_line_number : end_line_number + 1])
            segments.append(segment)

        return segments

    def __init__(self, lines):
        self._lines = lines

    @property
    def header(self):
        base_start = None
        head_start = None
        num_removed = 0
        num_added = 0
        num_context = 0

        for line in self.lines:
            if base_start is None and line.number["base"] is not None:
                base_start = int(line.number["base"])
            if head_start is None and line.number["head"] is not None:
                head_start = int(line.number["head"])
            if line.added:
                num_added += 1
            elif line.removed:
                num_removed += 1
            else:
                num_context += 1

        return (
            base_start or 0,
            num_context + num_removed,
            head_start or 0,
            num_context + num_added,
        )

    @property
    def lines(self):
        return self._lines

    @property
    def has_unintended_changes(self):
        for line in self.lines:
            head_coverage = line.coverage["base"]
            base_coverage = line.coverage["head"]
            if not (line.added or line.removed) and (base_coverage != head_coverage):
                return True
        return False


class FileComparison:
    def __init__(
        self,
        base_file,
        head_file,
        diff_data=None,
        src=[],
        bypass_max_diff=False,
        should_search_for_changes=None,
    ):
        """
        comparison -- the enclosing Comparison object that owns this FileComparison

        base_file -- the ReportFile for this file from the base report

        head_file -- the ReportFile for this file from the head report

        diff_data -- the git-comparison between the base and head references in the instantiation
            Comparison object. fields include:

            stats: -- {"added": number of added lines, "removed": number of removed lines}
            segments: (described in detail in the FileComparisonTraverseManager docstring)
            before: the name of this file in the base reference, if different from name in head ref

            If this file is unchanged in the comparison between base and head, the default will be used.

        src -- The full source of the file in the head reference. Used in FileComparisonTraverseManager
            to join src-code with coverage data. Default is used when retrieving full comparison,
            whereas full-src is serialized when retrieving individual file comparison.

        bypass_max_diff -- configuration paramater that tells this class to ignore max-diff truncating.
            default is used when retrieving full comparison; True is passed when fetching individual
            file comparison.

        should_search_for_changes -- flag that indicates if this FileComparison has unexpected coverage changes,
            according to a value cached during asynchronous processing. Has three values:
            1. True - indicates this FileComparison has unexpected coverage changes according to worker,
                and we should process the lines in this FileComparison using FileComparisonTraverseManager
                to calculate a change summary.
            2. False - indicates this FileComparison does not have unexpected coverage changes according to
                worker, and we should not traverse this file or calculate a change summary.
            3. None (default) - indicates we do not have information cached from worker to rely on here
                (no value in cache), so we need to traverse this FileComparison and calculate a change
                summary to find out.
        """
        self.base_file = base_file
        self.head_file = head_file
        self.diff_data = diff_data
        self.src = src

        # Some extra fields for truncating large diffs in the initial response
        self.total_diff_length = (
            functools.reduce(
                lambda a, b: a + b,
                [len(segment["lines"]) for segment in self.diff_data["segments"]],
            )
            if self.diff_data is not None and self.diff_data.get("segments")
            else 0
        )

        self.bypass_max_diff = bypass_max_diff
        self.should_search_for_changes = should_search_for_changes

    @property
    def name(self):
        return {
            "base": self.base_file.name if self.base_file is not None else None,
            "head": self.head_file.name if self.head_file is not None else None,
        }

    @property
    def totals(self):
        head_totals = self.head_file.totals if self.head_file is not None else None

        # The call to '.apply_diff()' in 'Comparison.head_report' stores diff totals
        # for each file in the diff_data for that file (in a field called 'totals').
        # Here we pass this along to the frontend by assigning the diff totals
        # to the head_totals' 'diff' attribute. It is absolutely worth considering
        # modifying the behavior of shared.reports to implement something similar.
        if head_totals and self.diff_data:
            head_totals.diff = self.diff_data.get("totals", 0)
        return {
            "base": self.base_file.totals if self.base_file is not None else None,
            "head": head_totals,
        }

    @property
    def has_diff(self):
        return self.diff_data is not None

    @property
    def stats(self):
        return self.diff_data["stats"] if self.diff_data else None

    @cached_property
    def _calculated_changes_and_lines(self):
        """
        Applies visitors to the file to generate response data (line comparison representations
        and change summary). Only applies visitors if

          1. The file has a diff or src, in which case we need to generate response data for it anyway, or
          2. The should_search_for_changes flag is defined (not None) and is True

        This limitation improves performance by limiting searching for changes to only files that
        have them.
        """
        change_summary_visitor = CreateChangeSummaryVisitor(
            self.base_file, self.head_file
        )
        create_lines_visitor = CreateLineComparisonVisitor(
            self.base_file, self.head_file
        )

        if self.diff_data or self.src or self.should_search_for_changes is not False:
            FileComparisonTraverseManager(
                head_file_eof=self.head_file.eof if self.head_file is not None else 0,
                base_file_eof=self.base_file.eof if self.base_file is not None else 0,
                segments=self.diff_data["segments"]
                if self.diff_data and "segments" in self.diff_data
                else [],
                src=self.src,
            ).apply([change_summary_visitor, create_lines_visitor])

        return change_summary_visitor.summary, create_lines_visitor.lines

    @cached_property
    def change_summary(self):
        return self._calculated_changes_and_lines[0]

    @property
    def has_changes(self):
        return any(self.change_summary.values())

    @cached_property
    def lines(self):
        if self.total_diff_length > MAX_DIFF_SIZE and not self.bypass_max_diff:
            return None
        return self._calculated_changes_and_lines[1]

    @cached_property
    def segments(self):
        return Segment.segments(self)


report_service = ReportService()


class Comparison(object):
    def __init__(self, user, base_commit, head_commit):
        self.user = user
        self._base_commit = base_commit
        self._head_commit = head_commit

    def validate(self):
        # make sure head and base reports exist (will throw an error if not)
        self.head_report
        self.base_report

    @cached_property
    def base_commit(self):
        return self._base_commit

    @cached_property
    def head_commit(self):
        return self._head_commit

    @cached_property
    def files(self):
        for file_name in self.head_report.files:
            yield self.get_file_comparison(file_name)

    def get_file_comparison(self, file_name, with_src=False, bypass_max_diff=False):
        head_file = self.head_report.get(file_name)
        diff_data = self.git_comparison["diff"]["files"].get(file_name)

        if self.base_report is not None:
            base_file = self.base_report.get(file_name)
            if base_file is None and diff_data:
                base_file = self.base_report.get(diff_data.get("before"))
        else:
            base_file = None

        if with_src:
            adapter = RepoProviderService().get_adapter(
                user=self.user, repo=self.base_commit.repository
            )
            file_content = async_to_sync(adapter.get_source)(
                file_name, self.head_commit.commitid
            )["content"]
            # make sure the file is str utf-8
            if type(file_content) is not str:
                file_content = str(file_content, "utf-8")
            src = file_content.splitlines()
        else:
            src = []

        return FileComparison(
            base_file=base_file,
            head_file=head_file,
            diff_data=diff_data,
            src=src,
            bypass_max_diff=bypass_max_diff,
        )

    @property
    def git_comparison(self):
        return self._fetch_comparison_and_reverse_comparison[0]

    @cached_property
    def base_report(self):
        try:
            return report_service.build_report_from_commit(self.base_commit)
        except minio.error.NoSuchKey:
            raise MissingComparisonReport("Missing base report")

    @cached_property
    def head_report(self):
        try:
            report = report_service.build_report_from_commit(self.head_commit)
        except minio.error.NoSuchKey:
            raise MissingComparisonReport("Missing head report")

        report.apply_diff(self.git_comparison["diff"])
        return report

    @property
    def totals(self):
        return {
            "base": self.base_report.totals if self.base_report is not None else None,
            "head": self.head_report.totals if self.head_report is not None else None,
        }

    @property
    def git_commits(self):
        return self.git_comparison["commits"]

    @property
    def upload_commits(self):
        """
        Returns the commits that have uploads between base and head.
        :return: Queryset of core.models.Commit objects
        """
        commit_ids = [commit["commitid"] for commit in self.git_commits]
        commits_queryset = Commit.objects.filter(
            commitid__in=commit_ids, repository=self.base_commit.repository
        )
        commits_queryset.exclude(deleted=True)
        return commits_queryset

    @cached_property
    def _fetch_comparison_and_reverse_comparison(self):
        """
        Fetches comparison and reverse comparison concurrently, then
        caches the result. Returns (comparison, reverse_comparison).
        """
        adapter = RepoProviderService().get_adapter(
            self.user, self.base_commit.repository
        )
        comparison_coro = adapter.get_compare(
            self.base_commit.commitid, self.head_commit.commitid
        )

        reverse_comparison_coro = adapter.get_compare(
            self.head_commit.commitid, self.base_commit.commitid
        )

        async def runnable():
            return await asyncio.gather(comparison_coro, reverse_comparison_coro)

        return async_to_sync(runnable)()

    def flag_comparison(self, flag_name):
        return FlagComparison(self, flag_name)

    @property
    def non_carried_forward_flags(self):
        flags_dict = self.head_report.flags
        return [flag for flag, vals in flags_dict.items() if not vals.carriedforward]

    @cached_property
    def has_unmerged_base_commits(self):
        """
        We use reverse comparison to detect if any commits exist in the
        base reference but not in the head reference. We use this information
        to show a message in the UI urging the user to integrate the changes
        in the base reference in order to see accurate coverage information.
        We compare with 1 because torngit injects the base commit into the commits
        array because reasons.
        """
        return len(self._fetch_comparison_and_reverse_comparison[1]["commits"]) > 1


class FlagComparison(object):
    def __init__(self, comparison, flag_name):
        self.comparison = comparison
        self.flag_name = flag_name

    @cached_property
    def head_report(self):
        return self.comparison.head_report.flags.get(self.flag_name)

    @cached_property
    def base_report(self):
        return self.comparison.base_report.flags.get(self.flag_name)

    @cached_property
    def diff_totals(self):
        if self.head_report is None:
            return None
        git_comparison = self.comparison.git_comparison
        return self.head_report.apply_diff(git_comparison["diff"])


@dataclass
class ImpactedFile:
    base_name: str
    head_name: str
    base_coverage: ReportTotals
    head_coverage: ReportTotals
    patch_coverage: ReportTotals
    change_coverage: float


"""
This class creates helper methods relevant to the report created for comparison between two commits.

This class takes an existing comparison as the parameter and outputs logic relevant to any contents within it.
"""


class ComparisonReport(object):
    def __init__(self, comparison):
        self.comparison = comparison

    @cached_property
    def files(self):
        if not self.comparison.report_storage_path:
            return []
        report_data = self.get_comparison_data_from_archive()
        return report_data.get("files", [])

    def file(self, path):
        for file in self.files:
            if file["head_name"] == path:
                return file

    def impacted_file(self, path):
        impacted_file = self.file(path)
        return self.deserialize_file(impacted_file)

    def impacted_files(self, filters):
        impacted_files = self.files
        impacted_files = [self.deserialize_file(file) for file in impacted_files]
        return self._apply_filters(impacted_files, filters)

    def _apply_filters(self, impacted_files, filters):
        filter_parameter = filters.get("ordering", {}).get("parameter")
        filter_direction = filters.get("ordering", {}).get("direction")
        if filter_parameter and filter_direction:
            parameter_value = filter_parameter.value
            direction_value = filter_direction.value
            impacted_files = self.sort_impacted_files(
                impacted_files, parameter_value, direction_value
            )
        return impacted_files

    """
    Sorts the impacted files by any provided parameter and slides items with None values to the end
    """

    def sort_impacted_files(self, impacted_files, parameter_value, direction_value):
        # Separate impacted files with None values for the specified parameter value
        files_with_coverage = []
        files_without_coverage = []
        for file in impacted_files:
            if getattr(file, parameter_value):
                files_with_coverage.append(file)
            else:
                files_without_coverage.append(file)

        # Sort impacted_files list based on parameter value
        is_reversed = direction_value == "descending"
        files_with_coverage = sorted(
            files_with_coverage,
            key=lambda x: getattr(x, parameter_value),
            reverse=is_reversed,
        )

        # Merge both lists together
        return files_with_coverage + files_without_coverage

    """
    Fetches contents of the report
    """

    def get_comparison_data_from_archive(self):
        repository = self.comparison.compare_commit.repository
        archive_service = ArchiveService(repository)
        try:
            data = archive_service.read_file(self.comparison.report_storage_path)
            return json.loads(data)
        # pylint: disable=W0702
        except:
            log.error(
                "ComparisonReport - couldnt fetch data from storage", exc_info=True
            )
            return {}

    """
    Aggregates hits, misses and partials correspondent to the diff
    """

    def compute_patch_per_file(self, file):
        added_diff_coverage = file.get("added_diff_coverage", [])
        if not added_diff_coverage:
            return None
        patch_coverage = {"hits": 0, "misses": 0, "partials": 0}
        for added_coverage in added_diff_coverage:
            [_, type_coverage] = added_coverage
            if type_coverage == "h":
                patch_coverage["hits"] += 1
            if type_coverage == "m":
                patch_coverage["misses"] += 1
            if type_coverage == "p":
                patch_coverage["partials"] += 1
        return patch_coverage

    def deserialize_totals(self, file, key):
        if not file.get(key):
            return
        # convert dict to ReportTotals and compute the coverage
        totals = ReportTotals(**file[key])
        nb_branches = totals.hits + totals.misses + totals.partials
        totals.coverage = (100 * totals.hits / nb_branches) if nb_branches > 0 else None
        file[key] = totals

    """
    Extracts relevant data from the fiels to be exposed as an impacted file
    """

    def deserialize_file(self, file):
        file["patch_coverage"] = self.compute_patch_per_file(file)
        self.deserialize_totals(file, "base_coverage")
        self.deserialize_totals(file, "head_coverage")
        self.deserialize_totals(file, "patch_coverage")
        change_coverage = self.calculate_change(
            file["head_coverage"], file["base_coverage"]
        )
        return ImpactedFile(
            head_name=file["head_name"],
            base_name=file["base_name"],
            head_coverage=file["head_coverage"],
            base_coverage=file["base_coverage"],
            patch_coverage=file["patch_coverage"],
            change_coverage=change_coverage,
        )

    # TODO: I think this can be a function located elsewhere
    def calculate_change(self, head_coverage, compared_to_coverage):
        if head_coverage and compared_to_coverage:
            return head_coverage.coverage - compared_to_coverage.coverage
        # if not head_coverage:
        #     # return there is no head coverage
        # if not compared_to_coverage:
        #     # return there is no base coverage
        return None


class PullRequestComparison(Comparison):
    """
    A Comparison instantiated with a Pull. Contains relevant additional processing
    required for Pulls, including caching of files-with-changes and support for
    'pseudo-comparisons'.
    """

    def __init__(self, user, pull):
        self.pull = pull
        super().__init__(
            user=user,
            # these are lazy loaded in the property methods below
            base_commit=None,
            head_commit=None,
        )

    @cached_property
    def base_commit(self):
        try:
            return Commit.objects.get(
                repository=self.pull.repository,
                commitid=self.pull.compared_to
                if self.is_pseudo_comparison
                else self.pull.base,
            )
        except Commit.DoesNotExist:
            raise MissingComparisonCommit("Missing base commit")

    @cached_property
    def head_commit(self):
        try:
            return Commit.objects.get(
                repository=self.pull.repository, commitid=self.pull.head
            )
        except Commit.DoesNotExist:
            raise MissingComparisonCommit("Missing head commit")

    @cached_property
    def _files_with_changes_hash_key(self):
        return "/".join(
            (
                "compare-changed-files",
                self.pull.repository.author.service,
                self.pull.repository.author.username,
                self.pull.repository.name,
                f"{self.pull.pullid}",
            )
        )

    @cached_property
    def _files_with_changes(self):
        try:
            key = self._files_with_changes_hash_key
            changes = json.loads(redis.get(key) or json.dumps(None))
            log.info(
                f"Found {len(changes) if changes else 0} files with changes in cache.",
                extra=dict(repoid=self.pull.repository.repoid, pullid=self.pull.pullid),
            )
            return changes
        except OSError as e:
            log.warning(
                f"Error connecting to redis: {e}",
                extra=dict(repoid=self.pull.repository.repoid, pullid=self.pull.pullid),
            )

    def _set_files_with_changes_in_cache(self, files_with_changes):
        redis.set(
            self._files_with_changes_hash_key,
            json.dumps(files_with_changes),
            ex=86400,  # 1 day in seconds
        )
        log.info(
            f"Stored {len(files_with_changes)} files with changes in cache",
            extra=dict(repoid=self.pull.repository.repoid, pullid=self.pull.pullid),
        )

    @cached_property
    def files(self):
        """
        Overrides the 'files' property to do additional caching of
        'files_with_changes', for future performance improvements.
        """
        files_with_changes = []
        for file_comparison in super().files:
            if file_comparison.change_summary:
                files_with_changes.append(file_comparison.name["head"])
            yield file_comparison
        self._set_files_with_changes_in_cache(files_with_changes)

    def get_file_comparison(self, file_name, with_src=False, bypass_max_diff=False):
        """
        Overrides the 'get_file_comparison' method to set the "should_search_for_changes"
        field.
        """
        file_comparison = super().get_file_comparison(
            file_name, with_src=with_src, bypass_max_diff=bypass_max_diff
        )
        file_comparison.should_search_for_changes = (
            file_name in self._files_with_changes
            if self._files_with_changes is not None
            else None
        )
        return file_comparison

    @cached_property
    def is_pseudo_comparison(self):
        """
        Returns True if this comparison is a pseudo-comparison, False if not.

        Depends on
            1) The repository yaml or app yaml settings allow pseudo_comparisons
            2) the pull request's 'compared_to' field is defined
        """
        return walk(
            _dict=self.pull.repository.yaml,
            keys=("codecov", "allow_pseudo_compare"),
            _else=get_config(("site", "codecov", "allow_pseudo_compare"), default=True),
        ) and bool(self.pull.compared_to)

    @cached_property
    def allow_coverage_offsets(self):
        """
        Returns True if "coverage offsets" are allowed, False if not, according
        to repository yaml settings or app yaml settings if not defined in repository
        yaml settings.
        """
        return walk(
            _dict=self.pull.repository.yaml,
            keys=("codecov", "allow_coverage_offsets"),
            _else=get_config(
                ("site", "codecov", "allow_coverage_offsets"), default=False
            ),
        )

    @cached_property
    def pseudo_diff(self):
        """
        Returns the diff between the 'self.pull.compared_to' field and the
        'self.pull.base' field.
        """
        adapter = RepoProviderService().get_adapter(self.user, self.pull.repository)
        return async_to_sync(adapter.get_compare)(
            self.pull.compared_to, self.pull.base
        )["diff"]

    @cached_property
    def pseudo_diff_adjusts_tracked_lines(self):
        """
        Returns True if we are doing a pull request pseudo-comparison, and tracked
        lines have changed between the pull's 'base' and 'compared_to' fields. This
        signifies an error-condition for the comparison, I think because if tracked lines
        have been adjusted between the 'base' and 'compared_to' commits, the 'compared_to'
        report can't be substituted for the 'base' report, since it will throw off the
        unexpected coverage change results. If `self.allow_coverage_offests` is True,
        client code can adjust the lines in the base report according to the diff
        with `self.update_base_report_with_pseudo_diff'.

        Ported from the block at: https://github.com/codecov/codecov.io/blob/master/app/handlers/compare.py#L137
        """
        if (
            self.is_pseudo_comparison
            and self.pull.base != self.pull.compared_to
            and self.base_report is not None
            and self.head_report is not None
        ):
            if self.pseudo_diff and self.pseudo_diff.get("files"):
                return self.base_report.does_diff_adjust_tracked_lines(
                    self.pseudo_diff,
                    future_report=self.head_report,
                    future_diff=self.git_comparison["diff"],
                )
        return False

    def update_base_report_with_pseudo_diff(self):
        self.base_report.shift_lines_by_diff(self.pseudo_diff, forward=True)


def recalculate_comparison(comparison: CommitComparison) -> None:
    if comparison.state != CommitComparison.CommitComparisonStates.PENDING:
        comparison.state = CommitComparison.CommitComparisonStates.PENDING
        comparison.save()
    TaskService().compute_comparison(comparison.id)
