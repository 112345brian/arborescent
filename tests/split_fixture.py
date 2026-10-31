"""#57: a synthetic monorepo for the split acceptance test. Plain data, no behaviour (a module so other suites can reuse it)."""

from __future__ import annotations

from dataclasses import dataclass, field

PURE = '["__future__", "dataclasses", "typing", "collections"]'

POLICY = f"""\
sources = ["app"]
rings = ["domain", "ports", "adapters", "composition"]
package_catalog = "packages.toml"
exclude = ["split"]  # where the harness stages the extracted parts

[groups]
pure = {PURE}

[ring.domain]
external = ["@pure"]
[ring.ports]
may_import = ["domain"]
external = ["@pure"]
[ring.adapters]
may_import = ["domain", "ports"]
external = ["@pure"]
[ring.composition]
may_import = ["*"]
external = ["@pure"]

[classify]
"app" = "domain"
"app.kernel" = "domain"
"app.kernel.types" = "domain"
"app.kernel.ports" = "ports"
"app.billing" = "domain"
"app.billing.models" = "domain"
"app.billing.internal" = "domain"
"app.billing.api" = "ports"
"app.billing.gateway" = "adapters"
"app.orders" = "domain"
"app.orders.model" = "domain"
"app.orders.service" = "adapters"
"app.shell" = "composition"
"app.shell.main" = "composition"
"""

# The deliberate blocking edge: orders reaches a billing module that billing keeps private.
BLOCKING_IMPORT = "from app.billing import internal"
CLEAN_IMPORT = "from app.billing import api"

FILES: dict[str, str] = {
    "app/__init__.py": "",
    "app/kernel/__init__.py": "",
    "app/kernel/types.py": "from __future__ import annotations\n\nMoney = int\n\n\nclass Id:\n    pass\n",
    "app/kernel/ports.py": (
        "from __future__ import annotations\n\nfrom app.kernel import types\n\n\n"
        "def new_id() -> types.Id:\n    return types.Id()\n"
    ),
    "app/billing/__init__.py": "",
    "app/billing/models.py": (
        "from __future__ import annotations\n\nfrom app.kernel import types\n\n\n"
        "class Invoice:\n    amount: types.Money = 0\n"
    ),
    "app/billing/internal.py": (
        "from __future__ import annotations\n\nfrom app.billing import models\n\n\n"
        "def ledger() -> list[models.Invoice]:\n    return []\n"
    ),
    "app/billing/api.py": (
        "from __future__ import annotations\n\nfrom app.billing import models\nfrom app.kernel import ports\n\n\n"
        "def invoice() -> models.Invoice:\n    ports.new_id()\n    return models.Invoice()\n"
    ),
    "app/billing/gateway.py": (
        "from __future__ import annotations\n\nfrom app.billing import api, internal\n\n\n"
        "def send() -> int:\n    return len(internal.ledger()) + (1 if api.invoice() else 0)\n"
    ),
    "app/billing/templates/invoice.html": "<h1>Invoice</h1>\n",
    "app/orders/__init__.py": "",
    "app/orders/model.py": (
        "from __future__ import annotations\n\nfrom app.kernel import types\n\n\n"
        "class Order:\n    total: types.Money = 0\n"
    ),
    "app/orders/service.py": (
        f"from __future__ import annotations\n\n{BLOCKING_IMPORT}\nfrom app.orders import model\n\n\n"
        "def place() -> model.Order:\n    return model.Order()\n"
    ),
    "app/shell/__init__.py": "",
    "app/shell/main.py": (
        "from __future__ import annotations\n\nfrom app.billing import gateway\nfrom app.orders import service\n\n\n"
        "def run() -> int:\n    service.place()\n    return gateway.send()\n"
    ),
    "pyproject.toml": '[project]\nname = "synthetic-monorepo"\ndependencies = []\n',
}


@dataclass(frozen=True)
class Spec:
    """One catalog entry. `public` lists are written sorted on purpose: the export sorts them and the part comparison is
    order-sensitive (schema gap)."""

    pid: str
    kind: str
    summary: str
    files: list[str]
    depends: list[str]
    public: list[str]
    home: str
    assets: list[str] = field(default_factory=list)


def _package(spec: Spec) -> str:
    lines = [
        f'[[packages]]\nid = "{spec.pid}"\nname = "{spec.pid.title()}"\nkind = "{spec.kind}"\nrepository = "example/monorepo"\nroot = "."',
        f'summary = "{spec.summary}"',
        f"depends_on = {spec.depends!r}".replace("'", '"'),
        f"public = {spec.public!r}".replace("'", '"'),
        "externals = []",
        spec.home,
    ]
    lines += [f'[[packages.paths]]\npath = "{path}"\nrole = "source"' for path in spec.files]
    lines += [f'[[packages.paths]]\npath = "{path}"\nrole = "assets"' for path in spec.assets]
    return "\n".join(lines) + "\n"


CATALOG = "\n".join([
    _package(Spec(
        "kernel", "library", "shared types and ports", depends=[],
        files=["app/__init__.py", "app/kernel/__init__.py", "app/kernel/types.py", "app/kernel/ports.py"],
        public=["app", "app.kernel", "app.kernel.ports", "app.kernel.types"],
        home='[packages.home]\nkind = "repository"\nrepository = "example/kernel"',  # the own-repository package
    )),
    _package(Spec(
        "billing", "library", "invoices; its modules span three rings", depends=["kernel"],
        files=["app/billing/__init__.py", "app/billing/models.py", "app/billing/internal.py", "app/billing/api.py",
               "app/billing/gateway.py"],
        assets=["app/billing/templates"],
        public=["app.billing", "app.billing.api", "app.billing.gateway", "app.billing.models"],
        home='[packages.home]\nkind = "workspace-member"\npath = "packages/billing"',  # the workspace member
    )),
    _package(Spec(
        "orders", "library", "orders; carries the blocking edge", depends=["kernel", "billing"],
        files=["app/orders/__init__.py", "app/orders/model.py", "app/orders/service.py"],
        public=["app.orders", "app.orders.model", "app.orders.service"],
        home='[packages.home]\nkind = "repository"\nrepository = "example/orders"',
    )),
    _package(Spec(
        "shell", "application", "entry point", depends=["billing", "orders"],
        files=["app/shell/__init__.py", "app/shell/main.py"],
        public=[],
        home='[packages.home]\nkind = "repository"\nrepository = "example/shell"',
    )),
])
