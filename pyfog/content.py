from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationItem:
    key: str
    href: str
    label: str
    icon: str
    permission: str


NAVIGATION = (
    NavigationItem(key="hosts", href="/hosts", label="Equipos", icon="▣", permission="hosts.read"),
    NavigationItem(
        key="pairing", href="/pairing", label="Descubiertos", icon="⌁", permission="pairing.read"
    ),
    NavigationItem(
        key="images", href="/images", label="Imágenes", icon="◈", permission="images.read"
    ),
    NavigationItem(key="tasks", href="/tasks", label="Tareas", icon="◷", permission="tasks.read"),
    NavigationItem(
        key="status", href="/status", label="Estado", icon="✓", permission="status.read"
    ),
    NavigationItem(
        key="audit", href="/audit", label="Auditoría", icon="≡", permission="audit.read"
    ),
    NavigationItem(
        key="users", href="/users", label="Usuarios", icon="◎", permission="users.manage"
    ),
)

SHELL_COPY = {
    "workspace_label": "ESPACIO DE TRABAJO",
    "first_stage": "PRIMERA ETAPA",
    "first_stage_title": "Conocé tu infraestructura.",
    "first_stage_description": "Registro e inventario de equipos Linux.",
    "administrator": "Administrador",
    "logout": "Cerrar sesión",
    "topbar": "Infraestructura Linux",
    "footer": "PyFog · Registro e inventario",
    "footer_tagline": "Tu infraestructura, bajo control.",
}
