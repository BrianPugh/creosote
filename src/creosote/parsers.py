import ast
import pathlib
from functools import lru_cache

import toml
from loguru import logger

from creosote.models import Import, Package
from dotty_dict import dotty
import re


class PackageReader:
    def __init__(self):
        self.packages = None

    def _pyproject_pep621(self, section_contents: dict):
        if not isinstance(section_contents, list):
            raise TypeError("Unexpected dependency format, list expected.")

        section_deps = []
        for dep in section_contents:
            match = re.match(r"([\w\-\_]*)[>=|==|>=]*", dep)
            if match and match.groups():
                dep = match.groups()[0]
                section_deps.append(dep)
        return section_deps

    def _pyproject_poetry(self, section_contents: dict):
        if not isinstance(section_contents, dict):
            raise TypeError("Unexpected dependency format, dict expected.")
        return section_contents.keys()

    def _pyproject(self, deps_file: str, sections: list):
        """Return dependencies from pyproject.toml."""
        with open(deps_file, "r") as infile:
            contents = toml.loads(infile.read())

        dotty_contents = dotty(contents)
        deps = []

        for section in sections:
            try:
                section_contents = dotty_contents[section]
            except KeyError as err:
                raise KeyError(f"Could not find toml section {section}.") from err
            section_deps = []

            if section.startswith("project"):
                section_deps = self._pyproject_pep621(section_contents)
            elif section.startswith("tool.poetry"):
                section_deps = self._pyproject_poetry(section_contents)
            else:
                raise TypeError("Unsupported dependency format.")

            if not section_deps:
                logger.warning(f"No dependencies found in section {section}")
            else:
                logger.info(
                    f"Dependencies found in {section}: {', '.join(section_deps)}"
                )
                deps.extend(section_deps)

        return sorted(deps)

    def _requirements(self, deps_file: str):
        """Return dependencies from requirements.txt-format file."""
        deps = []
        with open(deps_file, "r") as infile:
            contents = infile.readlines()

        for line in contents:
            if not line.startswith(" "):
                deps.append(line[: line.find("=")])

        return sorted(deps)

    @lru_cache(maxsize=None)  # noqa: B019
    def ignore_packages(self):
        return ["python"]

    def packages_sans_ignored(self, deps):
        packages = []
        for dep in deps:
            if dep not in self.ignore_packages():
                packages.append(Package(name=dep))
        return packages

    def read(self, deps_file: str, sections: list):
        if not pathlib.Path(deps_file).exists():
            raise Exception(f"File {deps_file} does not exist")

        if "pyproject.toml" in deps_file:
            self.packages = self.packages_sans_ignored(
                self._pyproject(deps_file, sections)
            )
        elif deps_file.endswith(".txt") or deps_file.endswith(".in"):
            self.packages = self.packages_sans_ignored(self._requirements(deps_file))
        else:
            raise NotImplementedError(
                f"Dependency specs file {deps_file} is not supported."
            )

        logger.info(
            f"Found packages in {deps_file}: "
            f"{', '.join([pkg.name for pkg in self.packages])}"
        )


def get_module_info_from_code(path):
    """Get imports, based on given filepath.

    Credit:
        https://stackoverflow.com/a/9049549/2448495
    """
    with open(path) as fh:
        root = ast.parse(fh.read(), path)

    for node in ast.iter_child_nodes(root):  # or potentially ast.walk ?
        if isinstance(node, ast.Import):
            module = []
        elif isinstance(node, ast.ImportFrom):
            module = node.module.split(".")
        else:
            continue

        for n in node.names:
            yield Import(module, n.name.split("."), n.asname)


def get_modules_from_code(paths):
    resolved_paths = []
    imports = []

    for path in paths:
        if pathlib.Path(path).is_dir():
            resolved_paths.extend(iter(pathlib.Path(path).glob("**/*.py")))
        else:
            resolved_paths.append(pathlib.Path(path).resolve())

    for resolved_path in resolved_paths:
        logger.info(f"Parsing {resolved_path}")
        for imp in get_module_info_from_code(resolved_path):
            imports.append(imp)

    dupes_removed = []
    for imp in imports:
        if imp not in dupes_removed:
            dupes_removed.append(imp)

    return dupes_removed
