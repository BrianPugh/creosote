import os
import pathlib
import re
from pathlib import Path
from typing import List

from distlib import database
from loguru import logger

from creosote.models import DependencyInfo, ImportInfo


class DepsResolver:
    def __init__(
        self,
        imports: List[ImportInfo],
        dependency_names: List[str],
        venv: str,
        excluded_deps_not_installed: List[str],
    ):
        self.imports = imports
        self.dependencies = [DependencyInfo(name=dep) for dep in dependency_names]
        self.venv = venv
        self.excluded_deps_not_installed = excluded_deps_not_installed

        self.map_dep_to_import_via_top_level_txt_file
        self.top_level_txt_pattern = re.compile(
            r"\/([\w]*).[\d\.]*.dist-info\/top_level.txt"
        )

        self.unused_deps: List[DependencyInfo] = []

    @staticmethod
    def canonicalize_module_name(module_name: str) -> str:
        return module_name.replace("-", "_").replace(".", "_").strip()

    def is_importable(self, module_name: str) -> bool:
        try:
            __import__(self.canonicalize_module_name(module_name))
            return True
        except ImportError:
            return False

    def gather_top_level_filepaths(self) -> None:
        """Gathers all top_level.txt filepaths in the venv.

        Note:
            The path may contain case sensitive variations of the
            dependency name, like e.g. GitPython for gitpython.
        """
        logger.debug("Gathering all top_level.txt files in venv...")
        venv_path = pathlib.Path(self.venv)
        glob_str = "**/*.dist-info/top_level.txt"
        top_level_filepaths = venv_path.glob(glob_str)
        self.top_level_filepaths = sorted(top_level_filepaths)
        for top_level_filepath in self.top_level_filepaths:
            logger.debug(f"Found {top_level_filepath}")

    def map_dep_to_import_via_top_level_txt_file(
        self, dep_info: DependencyInfo
    ) -> bool:
        """Map dependency to import via top_level.txt file.

        Return True if import name was found in the top_level.txt,
        otherwise return False.
        """
        dep_name = self.canonicalize_module_name(dep_info.name)

        for top_level_filepath in self.top_level_filepaths:
            matches = self.top_level_txt_pattern.findall(str(top_level_filepath))
            for import_name_from_top_level in matches:
                if import_name_from_top_level.lower() == dep_name.lower():
                    with open(top_level_filepath, "r", encoding="utf-8") as infile:
                        lines = infile.readlines()
                    dep_info.top_level_import_names = [line.strip() for line in lines]
                    import_names = ",".join(dep_info.top_level_import_names)
                    logger.debug(
                        f"[{dep_info.name}] found import name "
                        f"via top_level.txt: {import_names} ⭐️"
                    )
                    return True
        logger.debug(f"[{dep_info.name}] did not find top_level.txt in venv")
        return False

    def map_dep_to_module_via_distlib(self, dep_info: DependencyInfo) -> bool:
        """Fallback to distlib if we can't find the top_level.txt file.

        Return True if import name was found in the distlib database,
        otherwise return False.

        It seems this brings very little value right now, but I'll
        leave it in for now...
        """
        dp = database.DistributionPath(include_egg=True)
        dist = dp.get_distribution(dep_info.name)

        if dist is None:
            # raise ModuleNotFoundError
            logger.debug(
                f"[{dep_info.name}] did not find dependency in distlib.database"
            )
            return False

        # until we figure out something better... (not great)
        module_name = self.canonicalize_module_name(dep_info.name)

        for filename, _, _ in dist.list_installed_files():  # TODO: #125
            if filename.endswith((".py")):
                parts = os.path.splitext(filename)[0].split(os.sep)
                if len(parts) == 1:  # windows sep varies with distribution type
                    parts = os.path.splitext(filename)[0].split("/")
                if parts[-1].startswith("_") and not parts[-1].startswith("__"):
                    continue  # ignore internals
                elif filename.endswith(".py") and parts[-1] == "__init__":
                    module_name = parts[-2]
                    break

        logger.debug(
            f"[{dep_info.name}] found import name "
            f"via distlib.database: {module_name} 🤞"
        )
        dep_info.distlib_db_import_name = module_name
        return True

    def gather_import_info(self):
        """Populate DependencyInfo object with import naming info.

        There are three strategies from where the import name can be
        found:
            1. In the top_level.txt file in the venv.
            2. From the distlib database.
            3. Guess the import name by canonicalizing the dep name.

        Later, these gathered import names will be compared against the
        imports found in the source code by the AST parser.
        """
        logger.debug("Attempting to find import names...")
        venv_exists = Path(self.venv).exists()
        found_import_name = False

        if not venv_exists:
            logger.warning(
                f"Virtual environment '{self.venv}' does not exist, "
                "cannot resolve top-level names. This may lead to incorrect results."
            )

        for dep_info in self.dependencies:
            if venv_exists:
                # best chance to get the import name
                found_import_name = self.map_dep_to_import_via_top_level_txt_file(
                    dep_info
                )

            if not found_import_name:
                # fallback to distlib
                found_import_name = self.map_dep_to_module_via_distlib(dep_info)

            # this is really just guessing, but it's better than nothing
            dep_info.canonicalized_dep_name = self.canonicalize_module_name(
                dep_info.name
            )
            if not found_import_name:
                logger.debug(
                    f"[{dep_info.name}] relying on canonicalization "
                    f"fallback: {dep_info.canonicalized_dep_name } 🤞"
                )

    def associate_dep_with_import(self, dep_info: DependencyInfo, import_name: str):
        for imp in self.imports.copy():
            if not imp.module and import_name in imp.name:  # noqa: SIM114
                # import <imp.name>
                dep_info.associated_imports.append(imp)
            elif imp.name and import_name in imp.module:
                # from <imp.name> import ...
                dep_info.associated_imports.append(imp)

    def associate_dep_info_with_imports(self):
        """Associate dependency name with import (module) name.

        The AST has found imports from the source code. This function
        will now attempt to associate these imports with the
        DependencyInfo data, gathered from the venv, distlib, or
        canonicalization.
        """
        for dep_info in self.dependencies:
            if dep_info.top_level_import_names:
                for top_level_import_name in dep_info.top_level_import_names:
                    self.associate_dep_with_import(dep_info, top_level_import_name)
            elif dep_info.distlib_db_import_name:
                self.associate_dep_with_import(
                    dep_info, dep_info.distlib_db_import_name
                )
            elif dep_info.canonicalized_dep_name:
                self.associate_dep_with_import(
                    dep_info, dep_info.canonicalized_dep_name
                )

    def get_unused_dependencies(self) -> None:
        self.unused_deps = [
            dep_info
            for dep_info in self.dependencies
            if not dep_info.associated_imports
        ]

    def resolve_unused_dependency_names(self) -> List[str]:
        self.gather_top_level_filepaths()
        self.gather_import_info()
        self.associate_dep_info_with_imports()
        self.get_unused_dependencies()

        logger.debug(
            "Dependencies with populated 'associated_import' attribute are used in "
            "code. End result of resolve:"
        )
        for dep_info in self.dependencies:
            logger.debug(f"- {dep_info}")

        unused_dependency_names = sorted(
            [dep_info.name for dep_info in self.unused_deps]
            + self.excluded_deps_not_installed
        )

        return unused_dependency_names
