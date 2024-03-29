import copy
import json
import os
import re
import subprocess
from pathlib import Path

import llnl.util.tty as tty

import spack.environment as ev
import spack.hash_types as ht
import spack.util.spack_yaml as syaml
from spack.repo import PATH
from spack.spec import InstallStatus, Spec
from spack.traverse import traverse_nodes

from .config import user_config_dir, mpd_project_exists, project_config_from_args, update_config
from .util import bold


def setup_subparser(subparsers):
    default_top = Path.cwd()
    new_project = subparsers.add_parser(
        "new-project",
        description="create MPD development area",
        aliases=["n", "newDev"],
        help="create MPD development area",
    )
    new_project.add_argument("--name", required=True, help="(required)")
    new_project.add_argument(
        "-T",
        "--top",
        default=default_top,
        help="top-level directory for MPD area\n(default: %(default)s)",
    )
    new_project.add_argument(
        "-S",
        "--srcs",
        help="directory containing repositories to develop\n"
        "(default: <top-level directory>/srcs)",
    )
    new_project.add_argument(
        "-f", "--force", action="store_true", help="overwrite existing project with same name"
    )
    new_project.add_argument(
        "-E",
        "--env",
        help="environments from which to create project\n(multiple allowed)",
        action="append",
    )
    new_project.add_argument("variants", nargs="*")


def lint_spec(spec):
    spec_str = spec.short_spec
    spec_str = re.sub(f"@{spec.version}", "", spec_str)  # remove version
    spec_str = re.sub(f"arch={spec.architecture}", "", spec_str)  # remove arch
    spec_str = re.sub(f"%{spec.compiler.display_str}", "", spec_str)  # remove compiler
    spec_str = re.sub("/[a-z0-9]+", "", spec_str)  # remove hash
    if "patches" in spec.variants:  # remove patches if present
        spec_str = re.sub(f"{spec.variants['patches']}", "", spec_str)
    return spec_str.strip()


def entry(package_list, package_name):
    for p in package_list:
        if package_name == p["name"]:
            return p
    return None


def entry_with_index(package_list, package_name):
    for i, p in enumerate(package_list):
        if package_name == p["name"]:
            return i, p
    return None


def cmake_lists_preamble(package):
    return f"""cmake_minimum_required (VERSION 3.18.2 FATAL_ERROR)
project({package}-devel LANGUAGES NONE)

find_package(cetmodules REQUIRED)
include(CetCMakeEnv)

"""


def cmake_presets(source_path, dependencies, cxx_standard, preset_file):
    configurePresets, cacheVariables = "configurePresets", "cacheVariables"
    allCacheVariables = {
        "CMAKE_BUILD_TYPE": {"type": "STRING", "value": "RelWithDebInfo"},
        "CMAKE_CXX_EXTENSIONS": {"type": "BOOL", "value": "OFF"},
        "CMAKE_CXX_STANDARD_REQUIRED": {"type": "BOOL", "value": "ON"},
        "CMAKE_CXX_STANDARD": {"type": "STRING", "value": cxx_standard},
    }

    # Pull project-specific presets from each dependency.
    for dep in dependencies:
        pkg_presets_file = source_path / dep / "CMakePresets.json"
        if not pkg_presets_file.exists():
            continue

        with open(pkg_presets_file, "r") as f:
            pkg_presets = json.load(f)
            pkg_config_presets = pkg_presets[configurePresets]
            default_presets = next(
                filter(lambda s: s["name"] == "from_product_deps", pkg_config_presets)
            )
            for key, value in default_presets[cacheVariables].items():
                if key.startswith(dep):
                    allCacheVariables[key] = value

    presets = {
        configurePresets: [
            {
                cacheVariables: allCacheVariables,
                "description": "Configuration settings as created by 'spack mpd new-dev'",
                "displayName": "Configuration from mpd new-dev",
                "name": "default",
            }
        ],
        "version": 3,
    }
    return json.dump(presets, preset_file, indent=4)


def bundle_template(package, dependencies):
    camel_package = package.split("-")
    camel_package = "".join(word.title() for word in camel_package)
    bundle_str = f"""from spack.package import *
import spack.extensions


class {camel_package}(BundlePackage):
    "Bundle package for developing {package}"

    homepage = "[See https://...  for instructions]"

    version("develop")

"""
    for dep in dependencies:
        bundle_str += f'    depends_on("{dep}")\n'

    return bundle_str


def make_cmake_file(package, dependencies, project_config):
    source_path = Path(project_config["source"])
    with open((source_path / "CMakeLists.txt").absolute(), "w") as f:
        f.write(cmake_lists_preamble(package))
        for d in dependencies:
            f.write(f"add_subdirectory({d})\n")
        f.write("\nenable_testing()")

    with open((source_path / "CMakePresets.json").absolute(), "w") as f:
        cmake_presets(source_path, dependencies, project_config["cxxstd"], f)


def make_yaml_file(package, spec, prefix=None):
    filepath = Path(f"{package}.yaml")
    if prefix:
        filepath = prefix / filepath
    with open(filepath, "w") as f:
        syaml.dump(spec, stream=f, default_flow_style=False)
    return str(filepath)


def mpd_envs(name, project_config):
    return f"""

    def setup_run_environment(self, env):
        mpd = spack.extensions.get_module("mpd")
        mpd.make_active("{name}")

    @run_after("install")
    def post_install(self):
        mpd = spack.extensions.get_module("mpd")
        mpd.add_project({dict(**project_config)})
"""


def make_bundle_file(name, deps, project_config, mpd_project_name=None):
    bundle_path = user_config_dir() / "packages" / name
    bundle_path.mkdir(exist_ok=True)
    package_recipe = bundle_path / "package.py"
    with open(package_recipe.absolute(), "w") as f:
        f.write(bundle_template(name, deps))
        if mpd_project_name:
            f.write(mpd_envs(mpd_project_name, project_config))


def process_config(project_config):
    proto_env_packages_files = []
    proto_envs = []
    for penv_name in project_config["envs"]:
        proto_env = ev.read(penv_name)
        proto_envs.append(proto_env)
        proto_env_packages_config = dict(
            packages=proto_env.manifest.configuration.get("packages", {})
        )
        proto_env_packages_files.append(
            make_yaml_file(
                f"{penv_name}-packages-config", proto_env_packages_config, prefix=user_config_dir()
            )
        )

    print()
    tty.msg("Concretizing project (this may take a few minutes)")

    name = project_config["name"]
    spec_like = (
        f"{name}-bootstrap@develop %{project_config['compiler']} {project_config['variants']}"
    )
    spec = Spec(spec_like)

    bootstrap_name = spec.name

    concretized_spec = spec.concretized()

    packages_to_develop = project_config["packages"]
    ordered_dependencies = [
        p.name for p in concretized_spec.traverse(order="topo") if p.name in packages_to_develop
    ]
    ordered_dependencies.reverse()

    make_cmake_file(name, ordered_dependencies, project_config)

    # YAML file
    spec_dict = concretized_spec.to_dict(ht.dag_hash)
    nodes = spec_dict["spec"]["nodes"]

    top_level_package = entry(nodes, bootstrap_name)
    assert top_level_package

    package_names = [dep["name"] for dep in top_level_package["dependencies"]]
    packages = {dep["name"]: dep for dep in top_level_package["dependencies"]}

    for pname in package_names:
        i, p = entry_with_index(nodes, pname)
        assert p

        pdeps = {pdep["name"]: pdep for pdep in p["dependencies"]}
        packages.update(pdeps)
        del nodes[i]

    for pname in package_names:
        del packages[pname]

    deps_for_bundlefile = []
    packages_block = {}
    for p in concretized_spec.traverse():
        if p.name not in packages:
            continue

        deps_for_bundlefile.append(p.name)

        if any(penv.matching_spec(p) for penv in proto_envs):
            continue

        requires = []
        if version := str(p.version):
            requires.append(f"@{version}")
        if compiler := str(p.compiler):
            requires.append(f"%{compiler}")
        if p.variants:
            variants = copy.deepcopy(p.variants)
            if "patches" in variants:
                del variants["patches"]
            requires.extend(str(variants).split())
        if compiler_flags := str(p.compiler_flags):
            requires.append(compiler_flags)

        packages_block[p.name] = dict(require=requires)

    # Always replace the bundle file
    mpd_name = name + "-mpd"
    make_bundle_file(mpd_name, deps_for_bundlefile, project_config, mpd_project_name=name)

    full_block = dict(
        include=proto_env_packages_files,
        definitions=[dict(compiler=[project_config["compiler"]])],
        specs=[project_config["compiler"], mpd_name],
        concretizer=dict(unify=True, reuse=True),
        packages=packages_block,
    )

    env_file = make_yaml_file(name, dict(spack=full_block), prefix=user_config_dir())
    env = ev.create(name, init_file=env_file)
    tty.info(f"Environment {name} has been created")

    concretized_specs = None
    with env.write_transaction():
        concretized_specs = env.concretize()
        env.write()

    absent_dependencies = []
    missing_intermediate_deps = {}
    for n in traverse_nodes([p[1] for p in concretized_specs], order="post"):
        if n.name == bootstrap_name:
            continue

        if n.install_status() == InstallStatus.absent:
            absent_dependencies.append(n.cshort_spec)

        missing_deps = [p.name for p in n.dependencies() if p.name in packages_to_develop]
        if missing_deps:
            missing_intermediate_deps[n.name] = missing_deps

    if missing_intermediate_deps:
        error_msg = (
            "\nThe following packages are intermediate dependencies and must"
            " also be checked out:\n\n"
        )
        for pkg_name, missing_deps in missing_intermediate_deps.items():
            missing_deps_str = ", ".join(missing_deps)
            error_msg += "      - " + bold(pkg_name)
            error_msg += f" (depends on {missing_deps_str})\n"
        error_msg += "\n"
        tty.die(error_msg)

    tty.msg("Concretization complete\n")

    msg = "Ready to install MPD project " + bold(name) + "\n"
    if absent_dependencies:

        def _parens_number(i):
            return f"({i})"

        msg += "\nThe following packages will be installed:\n"
        width = len(_parens_number(len(absent_dependencies)))
        for i, dep in enumerate(absent_dependencies):
            num_str = _parens_number(i + 1)
            msg += f"\n {num_str:>{width}}  {dep}"
        msg += "\n\nPlease ensure you have adequate space for these installations.\n"
    tty.msg(msg)

    should_install = tty.get_yes_or_no(
        "Would you like to continue with installation?", default=True
    )

    if should_install is False:
        print()
        tty.msg(
            f"To install {name} later, invoke:\n\n" + f"  spack -e {name} install -j<ncores>\n"
        )
        return

    ncores = tty.get_number("Specify number of cores to use", default=os.cpu_count() // 2)

    tty.msg(f"Installing {name}")
    result = subprocess.run(["spack", "-e", name, "install", f"-j{ncores}"])

    if result.returncode == 0:
        print()
        msg = (
            f"MPD project {bold(name)} has been installed.  "
            "To load it, invoke:\n\n  spack env activate {name}\n"
        )
        tty.msg(msg)


def print_config_info(config):
    print(f"\nUsing build area: {config['build']}")
    print(f"Using local area: {config['local']}")
    print(f"Using sources area: {config['source']}\n")
    packages = config["packages"]
    if not packages:
        return

    print("  Will develop:")
    for p in packages:
        print(f"    - {p}")


def prepare_project(project_config):
    build_dir = project_config["build"]
    bp = Path(build_dir)
    bp.mkdir(exist_ok=True)

    local_dir = project_config["local"]
    lp = Path(local_dir)
    lp.mkdir(exist_ok=True)

    local_install_path = Path(project_config["install"])
    local_install_path.mkdir(exist_ok=True)

    source_dir = project_config["source"]
    sp = Path(source_dir)
    sp.mkdir(exist_ok=True)


def concretize_project(project_config):
    packages_to_develop = project_config["packages"]

    # Always replace the bootstrap bundle file
    cxxstd = project_config["cxxstd"]
    packages_at_develop = []
    for p in packages_to_develop:
        # Check to see if packages support a 'cxxstd' variant
        spec = Spec(p)
        pkg_cls = PATH.get_pkg_class(spec.name)
        pkg = pkg_cls(spec)
        base_spec = f"{p}@develop"
        if "cxxstd" in pkg.variants:
            base_spec += f" cxxstd={cxxstd}"
        packages_at_develop.append(base_spec)

    make_bundle_file(project_config["name"] + "-bootstrap", packages_at_develop, project_config)

    process_config(project_config)


def declare_active(name):
    session_id = os.getsid(os.getpid())
    active = Path(user_config_dir() / "active")
    active.mkdir(exist_ok=True)
    with open(active / f"{session_id}", "w") as f:
        f.write(name + "\n")


def update_project(name, project_config):
    print()

    tty.msg(f"Updating project: {name}")
    print_config_info(project_config)

    if not project_config["packages"]:
        tty.msg(
            "No packages to develop.  You can clone repositories for development by invoking\n\n"
            "  spack mpd g --suite <suite name>\n\n"
            "  (or type 'spack mpd g --help' for more options)\n"
        )
        return

    concretize_project(name, project_config)


def process(args):
    print()

    name = args.name
    if mpd_project_exists(name):
        if args.force:
            tty.warn(f"Overwriting existing MPD project {bold(name)}")
            if ev.exists(name):
                env = ev.read(name)
                if env.active:
                    tty.die(f"Must deactivate environment {name} to overwrite it\n")
                env.destroy()
                tty.info(f"Existing environment {name} has been removed")
        else:
            indent = " " * len("==> Error: ")
            tty.die(
                f"An MPD project with the name {bold(name)} already exists.\n"
                f"{indent}Either choose a different name or use the '--force' option"
                " to overwrite the existing project.\n"
            )
    else:
        tty.msg(f"Creating project: {name}")

    project_config = project_config_from_args(args)
    update_config(project_config, installed=False)

    print_config_info(project_config)
    prepare_project(project_config)
    declare_active(project_config["name"])

    if len(project_config["packages"]):
        concretize_project(project_config)
    else:
        tty.msg(
            "You can clone repositories for development by invoking\n\n"
            "  spack mpd g --suite <suite name>\n\n"
            "  (or type 'spack mpd g --help' for more options)\n"
        )
