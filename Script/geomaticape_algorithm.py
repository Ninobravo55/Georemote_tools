from qgis.core import QgsProcessingAlgorithm
from qgis.PyQt.QtGui import QIcon
import os


class GeomaticapeAlgorithm(QgsProcessingAlgorithm):
    _algorithm_name = "default"
    _icon_name = "default.png"

    def name(self):
        return self._algorithm_name

    def icon(self):
        plugin_dir = os.path.dirname(os.path.dirname(__file__))
        return QIcon(os.path.join(plugin_dir, "Icons", self._icon_name))

    def createInstance(self):
        return self.__class__()

    def tr(self, string):
        from qgis.PyQt.QtCore import QCoreApplication
        return QCoreApplication.translate('GeomaticaPe', string)
