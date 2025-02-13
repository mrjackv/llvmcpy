import hashlib
import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import List, Optional

import platformdirs

from ._generator import Generator


def _get_version() -> str:
    if sys.version_info < (3, 8):
        import pkg_resources

        return pkg_resources.get_distribution(__name__).version
    else:
        from importlib.metadata import version

        return version(__name__)


class LLVMCPy:
    def __init__(self, llvm_config: Optional[str] = None):
        self._search_paths = os.environ.get("PATH", os.defpath).split(os.pathsep)
        if llvm_config is not None:
            self._llvm_config = llvm_config
        else:
            self._llvm_config = self._find_program("LLVM_CONFIG", ["llvm-config"])
        self._search_paths.insert(0, self._run_llvm_config(["--bindir"]))
        self.version = self._run_llvm_config(["--version"])

        module = self._get_module()
        for elem in dir(module):
            setattr(self, elem, getattr(module, elem))

    def _get_module(self):
        hash_obj = hashlib.sha256()
        hash_obj.update(self._llvm_config.encode("utf-8"))
        hash_obj.update(b"\x00" + _get_version().encode("utf-8"))

        dir_name = hash_obj.hexdigest() + "-" + self.version
        cache_dir = Path(platformdirs.user_cache_dir("llvmcpy")) / dir_name
        llvmcpyimpl_py = cache_dir / "llvmcpyimpl.py"
        if not llvmcpyimpl_py.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._make_wrapper(llvmcpyimpl_py)

        # These 3 lines are equivalent to importing `llvmcpyimpl_py` file but they:
        # * Do not require modifying sys.path
        # * Do not pollute the `sys.modules` dictionary, allowing for multiple
        #   versions of llvmcpy to be loaded at the same time
        spec = importlib.util.spec_from_file_location(f"llvmcpy-{dir_name}", llvmcpyimpl_py)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _make_wrapper(self, path: Path):
        cpp = self._find_program("CPP", ["clang", "cpp", "gcc", "cc"])

        if sys.platform == "win32":
            extension = ".dll"
        elif sys.platform == "darwin":
            extension = ".dylib"
        else:
            extension = ".so"

        libraries = []
        libdir_path = Path(self._run_llvm_config(["--libdir"]))
        shared_mode = self._run_llvm_config(["--shared-mode"])
        if shared_mode == "shared":
            # The names returned by `libnames` are `.so`s that can be used
            for libname in self._run_llvm_config(["--libnames"]).split(" "):
                lib_path = libdir_path / libname
                if extension in lib_path.suffixes:
                    libraries.append(lib_path)
        else:
            # Fallback solution, use glob. This is done because sometimes
            # llvm-config says it's static but also has shared libraries
            for lib_path in libdir_path.glob(f"libLLVM*{extension}*"):
                if lib_path.is_file() and not lib_path.is_symlink():
                    libraries.append(lib_path)

            if len(libraries) == 0 and shared_mode == "static":
                # Fallback solution #2, make a shared library out of the static one
                output_lib = path.parent / "libLLVM.so"
                args = [
                    cpp,
                    "-shared",
                    "-o",
                    str(output_lib.resolve()),
                    *shlex.split(self._run_llvm_config(["--ldflags"])),
                    # This command-line option is needed otherwise the linker
                    # would discard the unused symbols
                    "-Wl,--whole-archive",
                    *shlex.split(self._run_llvm_config(["--libs"])),
                    "-Wl,--no-whole-archive",
                    *shlex.split(self._run_llvm_config(["--system-libs"])),
                ]
                subprocess.check_call(args)
                libraries.append(output_lib)

        if len(libraries) == 0:
            raise ValueError(
                "No valid LLVM libraries found, LLVM must be built with BUILD_SHARED_LIBS"
            )

        include_dir = Path(self._run_llvm_config(["--includedir"]))
        generator = Generator(cpp, libraries, include_dir)
        generator.generate_wrapper(path)

    def _run_llvm_config(self, args: List[str]) -> str:
        """Invoke llvm-config with the specified arguments and return the output"""
        assert self._llvm_config is not None
        return subprocess.check_output([self._llvm_config, *args]).decode("utf-8").strip()

    def _find_program(self, env_variable: str, names: List[str]) -> str:
        """Find an executable in the env_variable environment variable or in PATH in
        with one of the names in the argument names."""

        for name in [os.environ.get(env_variable, ""), *names]:
            path = which(name, path=os.pathsep.join(self._search_paths))
            if path is not None:
                return path

        raise RuntimeError(
            f"Couldn't find {env_variable} or any of the following executables in PATH: "
            + " ".join(names)
        )
