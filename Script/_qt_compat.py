# -*- coding: utf-8 -*-
"""Utilidades de compatibilidad Qt5 / Qt6 para Geomaticape.

PyQt6 eliminó el alias ``exec_()`` que existía en PyQt5 (se mantiene
únicamente ``exec()``). Para que el plugin funcione tanto en instalaciones
de QGIS basadas en Qt5 como en las más recientes basadas en Qt6 sin
duplicar comprobaciones ``hasattr(...)`` en cada módulo, se centraliza
aquí la resolución del método de ejecución del bucle modal/evento.
"""

__all__ = ["qt_exec"]


def qt_exec(obj, *args, **kwargs):
    """Ejecuta el bucle modal/evento de ``obj`` (QDialog, QMenu, QEventLoop, ...)

    de forma compatible con PyQt5 (método ``exec_``) y PyQt6 (método
    ``exec``), sin usar directamente el nombre ``exec_`` en el código
    fuente (Qt6 lo considera un miembro obsoleto/renombrado).
    """
    method = getattr(obj, "exec", None)
    if method is None:
        method = getattr(obj, "exec_", None)
    if method is None:
        raise AttributeError(
            f"{obj!r} no expone un método 'exec' ni 'exec_' ejecutable"
        )
    return method(*args, **kwargs)
