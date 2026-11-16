"""The package command line shared by manage.py package and python -m skeleton.system.packages."""

from pathlib import Path
from urllib.parse import urljoin

from skeleton.versioning import core_version

from . import archive as artifacts
from . import limits
from .archive import extract_package, inspect_package
from .build import build_package, sign_package
from .catalog import build_catalog, catalog_entry, release_catalog_entry, validate_catalog
from .doctor import doctor
from .errors import _fail
from .gitref import exported_git_tree
from .manifest import load_manifest
from .primitives import _json, _object, canonical_json
from .release import build_release, inspect_release, package_kind, validate_release
from .signing import initialize_keys, load_trust_store
from .template import default_service_class, manifest_template


def configure_parser(parser):
    commands = parser.add_subparsers(dest="operation", required=True)
    keys = commands.add_parser("keygen", help="Generate a private Ed25519 key (0600) and public PEM.")
    keys.add_argument("--private-key", required=True)
    keys.add_argument("--public-key", required=True)
    init = commands.add_parser("init", help="Initialize the canonical manifest without overwriting files.")
    init.add_argument("--output", required=True)
    for flag in ("app-id", "app-label", "package-name", "publisher", "app-config-class"):
        init.add_argument("--" + flag, required=True)
    init.add_argument("--version", default="0.1.0")
    init.add_argument(
        "--service-class",
        action="append",
        default=[],
        help="Service API class of applications.<package>.service_api (default: <Package>Service).",
    )
    build = commands.add_parser("build", help="Build a complete signed .skapp, checking docs and wheels.")
    build.add_argument("--app-root", required=True)
    build.add_argument("--manifest", help="Manifest path (default: manifest.json of the app root).")
    build.add_argument(
        "--git-ref",
        help="Build from the committed tree at this git revision (git archive, honours export-ignore).",
    )
    build.add_argument("--output", required=True)
    build.add_argument("--private-key", required=True)
    build.add_argument("--wheel", action="append", default=[])
    release = commands.add_parser(
        "release-build", help="Wrap a signed core package with per-target locks, wheels and the agent wheel."
    )
    for name in ("core", "recipe", "output", "private-key"):
        release.add_argument("--" + name, required=True)
    release.add_argument("--agent-wheel")
    manifest = commands.add_parser("validate-manifest")
    manifest.add_argument("path")
    for name in ("validate", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("path")
        command.add_argument("--trust-store", required=name == "validate")
    sign = commands.add_parser("sign", help="Validate inventory then sign an existing app/core artifact.")
    sign.add_argument("path")
    sign.add_argument("--output", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--publisher")
    extract = commands.add_parser("extract")
    extract.add_argument("path")
    extract.add_argument("--trust-store", required=True)
    extract.add_argument("--destination")
    extract.add_argument("--wheels-destination")
    index = commands.add_parser("index", help="Build a signed expiring release index from trusted artifacts.")
    index.add_argument("artifacts", nargs="*")
    for name in ("output", "private-key", "trust-store", "publisher", "expires-at"):
        index.add_argument("--" + name, required=True)
    index.add_argument("--generation", type=int, required=True)
    index.add_argument("--channel", default="stable")
    index.add_argument("--artifact-base", default="")
    index.add_argument(
        "--previous", help="Previous signed index whose entries are kept (may be expired); its generation must grow."
    )
    validate_index = commands.add_parser("validate-index")
    validate_index.add_argument("path")
    validate_index.add_argument("--trust-store", required=True)
    validate_index.add_argument("--minimum-generation", type=int, default=0)
    validate_index.add_argument("--channel", action="append", default=[])
    diagnose = commands.add_parser(
        "doctor", help="Report every packaging, docs, migration and dependency problem of an app source."
    )
    diagnose.add_argument("app_root")
    diagnose.add_argument("--manifest")
    diagnose.add_argument("--git-ref", help="Diagnose the committed tree at this git revision (git archive).")
    diagnose.add_argument("--inventory", help="Target deployment inventory.json to resolve dependencies against.")
    diagnose.add_argument("--catalog", help="Signed release catalogue to look up dependency releases.")
    diagnose.add_argument("--trust-store", help="Trust store verifying --catalog.")
    diagnose.add_argument(
        "--bundled-app",
        action="append",
        default=[],
        help="First-party app installed in the target (name of SKELETON_BUNDLED_APPS); repeatable.",
    )
    versions = commands.add_parser("versions", help="Report core provenance and declared local app versions.")
    versions.add_argument("--manifest", action="append", default=[])
    return parser


def _trust(options):
    return load_trust_store(options["trust_store"])


def _optional_trust(options):
    return _trust(options) if options.get("trust_store") else None


def _run_keygen(options):
    private, public = initialize_keys(options["private_key"], options["public_key"])
    return {"private_key": str(private), "public_key": str(public)}


def _run_init(options):
    value = manifest_template(
        **{
            key: options[key]
            for key in ("app_id", "app_label", "package_name", "publisher", "version", "app_config_class")
        },
        service_classes=options.get("service_class") or [default_service_class(options["package_name"])],
    )
    with Path(options["output"]).open("xb") as handle:
        handle.write(canonical_json(value))
    return value


def _git_manifest(root, manifest, export):
    """The manifest inside the export: the same path relative to the app root."""
    if not manifest:
        return export / "manifest.json"
    source = Path(manifest).resolve()
    if not source.is_relative_to(Path(root).resolve()):
        _fail("With --git-ref the manifest must be inside the application root.")
    return export / source.relative_to(Path(root).resolve())


def _run_build(options):
    root, manifest, ref = options["app_root"], options.get("manifest"), options.get("git_ref")
    if ref:
        if Path(options["output"]).resolve().is_relative_to(Path(root).resolve()):
            _fail("Build output must be outside the application root.")
        with exported_git_tree(root, ref) as (export, commit):
            path = build_package(
                export,
                _git_manifest(root, manifest, export),
                options["output"],
                options["private_key"],
                options["wheel"],
            )
        return {"artifact": str(path), "commit": commit}
    path = build_package(
        root, manifest or Path(root) / "manifest.json", options["output"], options["private_key"], options["wheel"]
    )
    return {"artifact": str(path)}


def _recipe_targets(path):
    """The targets of a release recipe; relative paths are relative to the recipe file."""
    path = Path(path)
    if path.stat().st_size > 1024 * 1024:
        _fail("Release recipe is too large.")
    recipe = _object(_json(path.read_bytes()), ("name", "release_notes", "build", "targets"), ("targets",))
    if not isinstance(recipe["targets"], list):
        _fail("Release recipe targets must be a list.")
    base = path.parent
    targets = []
    for target in recipe["targets"]:
        _object(target, ("id", "environment", "lock", "wheels"), ("id", "environment", "lock", "wheels"))
        if not isinstance(target["lock"], str) or not isinstance(target["wheels"], str):
            _fail("Release recipe lock and wheels must be paths.")
        wheels = base / target["wheels"]
        if not wheels.is_dir():
            _fail("Release recipe wheels must be a directory.")
        targets.append(
            {
                "id": target["id"],
                "environment": target["environment"],
                "lock": base / target["lock"],
                "wheels": sorted(wheels.glob("*.whl"), key=lambda item: item.name),
            }
        )
    return recipe, targets


def _run_release_build(options):
    recipe, targets = _recipe_targets(options["recipe"])
    path = build_release(
        options["core"],
        options["output"],
        options["private_key"],
        targets=targets,
        agent_wheel=options.get("agent_wheel"),
        name=recipe.get("name", "Skeleton"),
        release_notes=recipe.get("release_notes", ""),
        build=recipe.get("build", ""),
    )
    return {"artifact": str(path)}


def _run_validate_manifest(options):
    return load_manifest(options["path"])


def _run_inspect(options):
    if package_kind(options["path"]) == "release":
        return inspect_release(options["path"], _optional_trust(options))
    return inspect_package(options["path"], _optional_trust(options))


def _run_sign(options):
    path = sign_package(options["path"], options["output"], options["private_key"], publisher=options.get("publisher"))
    return {"artifact": str(path)}


def _run_extract(options):
    roots = extract_package(
        artifacts.validate_package(options["path"], _trust(options)),
        options.get("destination"),
        wheels_destination=options.get("wheels_destination"),
    )
    return {key: str(value) for key, value in roots.items()}


def _previous_entries(path, store, *, publisher, generation):
    """Entries of the previous index, verified (expiry aside) and older than the new generation."""
    path = Path(path)
    if path.stat().st_size > limits.MAX_METADATA_BYTES:
        _fail("Catalog size limit exceeded.")
    envelope = _json(path.read_bytes())
    entries = envelope.get("catalog", {}).get("entries") if isinstance(envelope, dict) else None
    if not isinstance(entries, list) or not isinstance(envelope.get("catalog"), dict):
        _fail("Previous index is not a release catalog.")
    channels = {entry.get("channel") for entry in entries if isinstance(entry, dict)}
    body = validate_catalog(envelope, store, allow_expired=True, channels=tuple(channels))
    if body["publisher"] != publisher:
        _fail("Previous index belongs to another publisher.")
    if generation <= body["generation"]:
        _fail("Index generation must be greater than the previous index generation.")
    return body["entries"]


def _run_index(options):
    store = _trust(options)
    previous = options.get("previous")
    if not options["artifacts"] and not previous:
        _fail("At least one artifact is required without --previous.")
    entries = []
    if previous:
        entries = _previous_entries(previous, store, publisher=options["publisher"], generation=options["generation"])
    for artifact in options["artifacts"]:
        base = options["artifact_base"]
        reference = urljoin(base.rstrip("/") + "/", Path(artifact).name) if base else Path(artifact).name
        if package_kind(artifact) == "release":
            entry = release_catalog_entry(
                validate_release(artifact, store), artifact=reference, channel=options["channel"]
            )
        else:
            entry = catalog_entry(
                artifacts.validate_package(artifact, store), artifact=reference, channel=options["channel"]
            )
        identity = (entry["component_id"], entry["version"], entry["channel"])
        positions = [
            i for i, old in enumerate(entries) if (old["component_id"], old["version"], old["channel"]) == identity
        ]
        if positions:
            entries[positions[0]] = entry  # The new artifact replaces the entry of the same identity.
        else:
            entries.append(entry)
    path = build_catalog(
        entries,
        options["output"],
        options["private_key"],
        publisher=options["publisher"],
        generation=options["generation"],
        expires_at=options["expires_at"],
    )
    return {"index": str(path), "entries": len(entries), "generation": options["generation"]}


def _run_validate_index(options):
    return validate_catalog(
        options["path"],
        _trust(options),
        minimum_generation=options["minimum_generation"],
        channels=options["channel"] or ("stable",),
    )


def _run_doctor(options):
    def document(name):
        path = options.get(name)
        if not path:
            return None
        if Path(path).stat().st_size > 4 * 1024 * 1024:
            _fail(f"{name} is too large.")
        return _json(Path(path).read_bytes())

    if options.get("catalog") and not options.get("trust_store"):
        _fail("--catalog requires --trust-store.")
    arguments = {
        "inventory": document("inventory"),
        "catalog": document("catalog"),
        "trust_store": _optional_trust(options),
        "bundled_apps": options.get("bundled_app") or (),
    }
    root, ref = options["app_root"], options.get("git_ref")
    if ref:
        with exported_git_tree(root, ref) as (export, commit):
            return {
                **doctor(export, _git_manifest(root, options.get("manifest"), export), **arguments),
                "commit": commit,
            }
    return doctor(root, options.get("manifest"), **arguments)


def _run_versions(options):
    return {
        "core": core_version(),
        "apps": [
            {"app_id": m["app_id"], "version": m["version"], "origin": "project-local", "manifest": str(path)}
            for path in options["manifest"]
            for m in (load_manifest(path),)
            if m["kind"] == "app"
        ],
    }


# One handler per operation of configure_parser: options -> JSON-serializable result.
OPERATIONS = {
    "keygen": _run_keygen,
    "init": _run_init,
    "build": _run_build,
    "release-build": _run_release_build,
    "validate-manifest": _run_validate_manifest,
    "validate": _run_inspect,
    "inspect": _run_inspect,
    "sign": _run_sign,
    "extract": _run_extract,
    "index": _run_index,
    "validate-index": _run_validate_index,
    "doctor": _run_doctor,
    "versions": _run_versions,
}


def run_command(options):
    handler = OPERATIONS.get(options["operation"])
    if handler is None:
        _fail("Unknown package operation.")
    return handler(options)
