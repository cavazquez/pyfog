from dataclasses import dataclass


@dataclass(frozen=True)
class NavigationItem:
    key: str
    href: str
    label: str
    icon: str


@dataclass(frozen=True)
class UpcomingSection:
    key: str
    label: str
    eyebrow: str
    title: str
    description: str
    issue_number: int


NAVIGATION = (
    NavigationItem(key="hosts", href="/hosts", label="Equipos", icon="▣"),
    NavigationItem(key="images", href="/images", label="Imágenes", icon="◈"),
    NavigationItem(key="tasks", href="/tasks", label="Tareas", icon="◷"),
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

UPCOMING_SECTIONS = {
    "images": UpcomingSection(
        key="images",
        label="Imágenes",
        eyebrow="PRÓXIMA ENTREGA",
        title="Las imágenes llegan después del arranque por red.",
        description=(
            "El catálogo, la captura y la verificación de imágenes se habilitarán cuando el "
            "entorno PXE y el agente efímero estén listos."
        ),
        issue_number=22,
    ),
    "tasks": UpcomingSection(
        key="tasks",
        label="Tareas",
        eyebrow="PRÓXIMA ENTREGA",
        title="Las tareas aparecerán cuando exista un agente para ejecutarlas.",
        description=(
            "PyFog mostrará la cola y el progreso de capturas, restauraciones y clonaciones "
            "después de implementar la comunicación segura con el agente."
        ),
        issue_number=24,
    ),
}
