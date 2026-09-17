from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationItem:
    key: str
    href: str
    label: str
    icon: str


NAVIGATION = (
    NavigationItem(key="hosts", href="/hosts", label="Equipos", icon="▣"),
    NavigationItem(key="pairing", href="/pairing", label="Descubiertos", icon="⌁"),
    NavigationItem(key="images", href="/images", label="Imágenes", icon="◈"),
    NavigationItem(key="tasks", href="/tasks", label="Tareas", icon="◷"),
    NavigationItem(key="status", href="/status", label="Estado", icon="✓"),
    NavigationItem(key="audit", href="/audit", label="Auditoría", icon="≡"),
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
