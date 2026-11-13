"""The package command line shared by manage.py package and python -m skeleton.system.packages."""

from pathlib import Path

from skeleton.versioning import core_version

from . import archive as artifacts
from .archive import extract_package, inspect_package
from .build import build_package, sign_package
from .catalog import build_catalog, catalog_entry, validate_catalog
from .doctor import doctor
from .errors import _fail
from .manifest import default_service_class, load_manifest, manifest_template
from .primitives import _json, canonical_json
from .signing import initialize_keys, load_trust_store


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
    build.add_argument("--manifest", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--private-key", required=True)
    build.add_argument("--wheel", action="append", default=[])
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
    index.add_argument("artifacts", nargs="+")
    for name in ("output", "private-key", "trust-store", "publisher", "expires-at"):
        index.add_argument("--" + name, required=True)
    index.add_argument("--generation", type=int, required=True)
    index.add_argument("--channel", default="stable")
    index.add_argument("--artifact-base", default="")
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


def run_command(options):
    def trust():
        return load_trust_store(options["trust_store"])

    operation = options["operation"]
    if operation == "keygen":
        private, public = initialize_keys(options["private_key"], options["public_key"])
        return {"private_key": str(private), "public_key": str(public)}
    if operation == "init":
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
    if operation == "build":
        path = build_package(
            options["app_root"], options["manifest"], options["output"], options["private_key"], options["wheel"]
        )
        return {"artifact": str(path)}
    if operation == "validate-manifest":
        return load_manifest(options["path"])
    if operation in ("validate", "inspect"):
        return inspect_package(options["path"], trust() if options.get("trust_store") else None)
    if operation == "sign":
        path = sign_package(
            options["path"], options["output"], options["private_key"], publisher=options.get("publisher")
        )
        return {"artifact": str(path)}
    if operation == "extract":
        roots = extract_package(
            artifacts.validate_package(options["path"], trust()),
            options.get("destination"),
            wheels_destination=options.get("wheels_destination"),
        )
        return {key: str(value) for key, value in roots.items()}
    if operation == "index":
        from urllib.parse import urljoin

        store = trust()
        entries = []
        for artifact in options["artifacts"]:
            package = artifacts.validate_package(artifact, store)
            base = options["artifact_base"]
            reference = urljoin(base.rstrip("/") + "/", Path(artifact).name) if base else Path(artifact).name
            entries.append(catalog_entry(package, artifact=reference, channel=options["channel"]))
        path = build_catalog(
            entries,
            options["output"],
            options["private_key"],
            publisher=options["publisher"],
            generation=options["generation"],
            expires_at=options["expires_at"],
        )
        return {"index": str(path), "entries": len(entries)}
    if operation == "validate-index":
        return validate_catalog(
            options["path"],
            trust(),
            minimum_generation=options["minimum_generation"],
            channels=options["channel"] or ("stable",),
        )
    if operation == "doctor":

        def document(name):
            path = options.get(name)
            if not path:
                return None
            if Path(path).stat().st_size > 4 * 1024 * 1024:
                _fail(f"{name} is too large.")
            return _json(Path(path).read_bytes())

        if options.get("catalog") and not options.get("trust_store"):
            _fail("--catalog requires --trust-store.")
        return doctor(
            options["app_root"],
            options.get("manifest"),
            inventory=document("inventory"),
            catalog=document("catalog"),
            trust_store=trust() if options.get("trust_store") else None,
            bundled_apps=options.get("bundled_app") or (),
        )
    if operation == "versions":
        return {
            "core": core_version(),
            "apps": [
                {"app_id": m["app_id"], "version": m["version"], "origin": "project-local", "manifest": str(path)}
                for path in options["manifest"]
                for m in (load_manifest(path),)
                if m["kind"] == "app"
            ],
        }
    _fail("Unknown package operation.")
