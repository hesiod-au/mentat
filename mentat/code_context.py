from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent
from typing import Optional

from mentat.auto_context import get_feature_selector
from mentat.code_feature import (
    CodeFeature,
    CodeMessageLevel,
    count_feature_tokens,
    get_code_message_from_features,
    split_file_into_intervals,
)
from mentat.code_map import check_ctags_disabled
from mentat.diff_context import DiffContext
from mentat.embeddings import get_feature_similarity_scores
from mentat.git_handler import get_non_gitignored_files, get_paths_with_git_diffs
from mentat.include_files import (
    build_path_tree,
    get_ignore_files,
    get_include_files,
    is_file_text_encoded,
    print_invalid_path,
    print_path_tree,
)
from mentat.llm_api import count_tokens
from mentat.session_context import SESSION_CONTEXT
from mentat.session_stream import SessionStream
from mentat.utils import sha256


def _get_all_features(
    git_root: Path,
    include_files: dict[Path, list[CodeFeature]],
    ignore_files: set[Path],
    diff_context: DiffContext,
    code_map: bool,
    level: CodeMessageLevel,
    max_chars: int = 100000,
) -> list[CodeFeature]:
    """Return a list of all features in the git root with given properties."""

    all_features = list[CodeFeature]()
    for path in get_non_gitignored_files(git_root):
        abs_path = git_root / path
        if (
            abs_path.is_dir()
            or not is_file_text_encoded(abs_path)
            or abs_path in ignore_files
            or os.path.getsize(abs_path) > max_chars
        ):
            continue

        diff_target = diff_context.target if abs_path in diff_context.files else None
        user_included = abs_path in include_files
        if level == CodeMessageLevel.INTERVAL:
            # Return intervals if code_map is enabled, otherwise return the full file
            full_feature = CodeFeature(
                abs_path,
                level=CodeMessageLevel.CODE,
                diff=diff_target,
                user_included=user_included,
            )
            if not code_map:
                all_features.append(full_feature)
            else:
                _split_features = split_file_into_intervals(
                    git_root,
                    full_feature,
                    user_features=include_files.get(abs_path, []),
                )
                all_features += _split_features
        else:
            _feature = CodeFeature(
                abs_path, level=level, diff=diff_target, user_included=user_included
            )
            all_features.append(_feature)

    return sorted(all_features, key=lambda f: f.path.relative_to(git_root))


class CodeContext:
    include_files: dict[Path, list[CodeFeature]]
    ignore_files: set[Path]
    diff_context: DiffContext
    code_map: bool = True
    features: list[CodeFeature] = []
    diff: Optional[str] = None
    pr_diff: Optional[str] = None

    def __init__(
        self,
        stream: SessionStream,
        git_root: Path,
        diff: Optional[str] = None,
        pr_diff: Optional[str] = None,
    ):
        self.diff = diff
        self.pr_diff = pr_diff
        self.diff_context = DiffContext(stream, git_root, self.diff, self.pr_diff)
        # TODO: This is a dict so we can quickly reference either a path (key)
        # or the CodeFeatures (value) and their intervals. Redundant.
        self.include_files = {}
        self.ignore_files = set()

    def set_paths(
        self,
        paths: list[Path],
        exclude_paths: list[Path],
        ignore_paths: list[Path] = [],
    ):
        if not paths and (self.diff or self.pr_diff) and self.diff_context.files:
            paths = self.diff_context.files
        self.include_files, invalid_paths = get_include_files(paths, exclude_paths)
        for invalid_path in invalid_paths:
            print_invalid_path(invalid_path)
        self.ignore_files = get_ignore_files(ignore_paths)

    def set_code_map(self):
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        stream = session_context.stream

        if config.no_code_map:
            self.code_map = False
        else:
            disabled_reason = check_ctags_disabled()
            if disabled_reason:
                ctags_disabled_message = f"""
                    There was an error with your universal ctags installation, disabling CodeMap.
                    Reason: {disabled_reason}
                """
                ctags_disabled_message = dedent(ctags_disabled_message)
                stream.send(ctags_disabled_message, color="yellow")
                config.no_code_map = True
                self.code_map = False
            else:
                self.code_map = True

    def display_context(self):
        """Display the baseline context: included files and auto-context settings"""
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream
        config = session_context.config
        git_root = session_context.git_root

        stream.send("Code Context:", color="blue")
        prefix = "  "
        stream.send(f"{prefix}Directory: {git_root}")
        if self.diff_context.name:
            stream.send(f"{prefix}Diff:", end=" ")
            stream.send(self.diff_context.get_display_context(), color="green")
        if self.include_files:
            stream.send(f"{prefix}Included files:")
            stream.send(f"{prefix + prefix}{git_root.name}")
            print_path_tree(
                build_path_tree(list(self.include_files.keys()), git_root),
                get_paths_with_git_diffs(),
                git_root,
                prefix + prefix,
            )
        else:
            stream.send(f"{prefix}Included files: None", color="yellow")
        auto = config.auto_tokens
        if auto != 0:
            stream.send(
                f"{prefix}Auto-token limit:"
                f" {'Model max (default)' if auto is None else auto}"
            )
            stream.send(
                f"{prefix}CodeMaps: {'Enabled' if self.code_map else 'Disabled'}"
            )

    def display_features(self):
        """Display a summary of all active features"""
        session_context = SESSION_CONTEXT.get()
        stream = session_context.stream

        auto_features = {level: 0 for level in CodeMessageLevel}
        for f in self.features:
            if f.path not in self.include_files:
                auto_features[f.level] += 1
        if any(auto_features.values()):
            stream.send("Auto-Selected Features:", color="blue")
            for level, count in auto_features.items():
                if count:
                    stream.send(f"  {count} {level.description}")

    _code_message: str | None = None
    _code_message_checksum: str | None = None

    def _get_code_message_checksum(self, max_tokens: Optional[int] = None) -> str:
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        git_root = session_context.git_root
        code_file_manager = session_context.code_file_manager

        if not self.features:
            features_checksum = ""
        else:
            feature_files = {Path(git_root / f.path) for f in self.features}
            feature_file_checksums = [
                code_file_manager.get_file_checksum(f) for f in feature_files
            ]
            features_checksum = sha256("".join(feature_file_checksums))
        settings = {
            "code_map": self.code_map,
            "auto_tokens": config.auto_tokens,
            "use_embeddings": config.use_embeddings,
            "use_llm": self.use_llm,
            "diff": self.diff,
            "pr_diff": self.pr_diff,
            "max_tokens": max_tokens,
            "include_files": self.include_files,
        }
        settings_checksum = sha256(str(settings))
        return features_checksum + settings_checksum

    async def get_code_message(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        expected_edits: Optional[list[str]] = None,  # for training/benchmarking
    ) -> str:
        code_message_checksum = self._get_code_message_checksum(max_tokens)
        if (
            self._code_message is None
            or code_message_checksum != self._code_message_checksum
        ):
            self._code_message = await self._get_code_message(
                prompt, model, max_tokens, expected_edits
            )
            self._code_message_checksum = self._get_code_message_checksum(max_tokens)
        return self._code_message

    use_llm: bool = False
    auto_threshold: float = 0.0

    async def _get_code_message(
        self,
        prompt: str,
        model: str,
        max_tokens: int,
        expected_edits: Optional[list[str]] = None,
    ) -> str:
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        git_root = session_context.git_root

        # Setup code message metadata
        code_message = list[str]()
        self.diff_context.clear_cache()
        self.set_code_map()
        if self.diff_context.files:
            code_message += [
                "Diff References:",
                f' "-" = {self.diff_context.name}',
                ' "+" = Active Changes',
                "",
            ]
        code_message += ["Code Files:\n"]
        meta_tokens = count_tokens("\n".join(code_message), model)

        # Add user-included features
        included_features = self._get_include_features()
        included_feature_tokens = sum(
            await count_feature_tokens(included_features, model)
        )
        remaining_tokens = max(0, max_tokens - meta_tokens - included_feature_tokens)
        auto_tokens = config.auto_tokens
        if remaining_tokens == 0 or config.auto_tokens == 0:
            self.features = sorted(
                included_features, key=lambda f: f.path.relative_to(git_root)
            )
        else:
            auto_tokens = (
                remaining_tokens
                if auto_tokens is None
                else min(remaining_tokens, auto_tokens)
            )
            selectable_tokens = included_feature_tokens + auto_tokens

            # Get a complete, sorted list of features to select from
            if prompt and config.use_embeddings:
                candidate_features = await self.search(
                    prompt, level=CodeMessageLevel.INTERVAL
                )
                candidate_features = [
                    f[0] for f in candidate_features
                ]  # Drop the score, keep sorted
            else:
                candidate_features = _get_all_features(
                    git_root,
                    self.include_files,
                    self.ignore_files,
                    self.diff_context,
                    self.code_map,
                    CodeMessageLevel.INTERVAL,
                )
            _user = [f for f in candidate_features if f.user_included]
            _non = [f for f in candidate_features if not f.user_included]
            candidate_features = _user + _non  # Move included files to the front

            alt_levels = [CodeMessageLevel.FILE_NAME]
            if self.code_map:
                alt_levels = [
                    CodeMessageLevel.CMAP_FULL,
                    CodeMessageLevel.CMAP,
                ] + alt_levels

            feature_selector = get_feature_selector(self.use_llm)
            self.features = await feature_selector.select(
                candidate_features,
                selectable_tokens,
                model,
                alt_levels,
                prompt,
                expected_edits,
            )

        # Group intervals by file, separated by ellipses if there are gaps
        code_message += get_code_message_from_features(self.features)
        return "\n".join(code_message)

    def _get_include_features(self) -> list[CodeFeature]:
        session_context = SESSION_CONTEXT.get()
        git_root = session_context.git_root

        include_features = list[CodeFeature]()
        for path, features in self.include_files.items():
            annotations = self.diff_context.get_annotations(path)
            for feature in features:
                has_diff = any(a.intersects(feature.interval) for a in annotations)
                feature = CodeFeature(
                    feature.ref(),
                    feature.level,
                    diff=self.diff_context.target if has_diff else None,
                    user_included=True,
                )
                include_features.append(feature)

        def _feature_relative_path(f: CodeFeature) -> str:
            return os.path.relpath(f.path, git_root)

        return sorted(include_features, key=_feature_relative_path)

    def include_file(self, path: Path):
        paths, invalid_paths = get_include_files([path], [])
        for new_path, new_features in paths.items():
            if new_path not in self.include_files:
                self.include_files[new_path] = []
            for feature in new_features:
                self.include_files[new_path].append(feature)
        return list(paths.keys()), invalid_paths

    def exclude_file(self, path: Path):
        # TODO: Using get_include_files here isn't ideal; if the user puts in a glob that
        # matches files but doesn't match any files in context, we won't know what that glob is
        # and can't return it as an invalid path
        paths, invalid_paths = get_include_files([path], [])
        removed_paths = list[Path]()
        for new_path in paths.keys():
            if new_path in self.include_files:
                removed_paths.append(new_path)
                del self.include_files[new_path]
        return removed_paths, invalid_paths

    async def search(
        self,
        query: str,
        max_results: int | None = None,
        level: CodeMessageLevel = CodeMessageLevel.INTERVAL,
    ) -> list[tuple[CodeFeature, float]]:
        """Return the top n features that are most similar to the query."""
        session_context = SESSION_CONTEXT.get()
        config = session_context.config
        git_root = session_context.git_root
        stream = session_context.stream

        if not config.use_embeddings:
            stream.send(
                "Embeddings are disabled. Enable with `/config use_embeddings true`",
                color="light_red",
            )
            return []

        all_features = _get_all_features(
            git_root,
            self.include_files,
            self.ignore_files,
            self.diff_context,
            self.code_map,
            level,
        )
        sim_scores = await get_feature_similarity_scores(query, all_features)
        all_features_scored = zip(all_features, sim_scores)
        all_features_sorted = sorted(
            all_features_scored, key=lambda x: x[1], reverse=True
        )
        if max_results is None:
            return all_features_sorted
        else:
            return all_features_sorted[:max_results]
