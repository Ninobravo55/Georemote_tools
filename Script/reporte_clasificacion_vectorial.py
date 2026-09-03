from .geomaticape_algorithm import GeomaticapeAlgorithm
"""
Reporte de clasificacion vectorial
===================================
A partir de una capa vectorial clasificada (ej. la salida de la
clasificacion supervisada), agrupa las geometrias por un campo de
clase (NAME) y suma sus areas (Area_Ha).
Genera un reporte en CSV y graficos (barras y pastel).

Autor : Geomatica Ambiental - https://www.geomatica.pe
Plugin: Geomaticape
Grupo : PostProcesamiento
"""

import os
import csv
from qgis.core import (
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterField,
    QgsProcessingParameterFolderDestination,
    QgsProcessingException,
    QgsProcessing
)
from qgis import processing


class ReporteClasificacionVectorial(GeomaticapeAlgorithm):
    _algorithm_name = "reporte_clasificacion_vectorial"
    _icon_name = "zonal_raster.png"

    INPUT_VECTOR = "INPUT_VECTOR"
    FIELD_NAME = "FIELD_NAME"
    FIELD_AREA = "FIELD_AREA"
    OUT_FOLDER = "OUT_FOLDER"

    def displayName(self):
        return self.tr("Reporte clasificacion vectorial (area y graficos)")

    def group(self):
        return self.tr("PostProcesamiento")

    def groupId(self):
        return "geomaticape_postprocesamiento"

    def shortHelpString(self):
        return """
<h3>Reporte clasificacion vectorial</h3>
<b>Autor:</b> GEOMATICA AMBIENTAL<br>
<b>Plugin:</b> Geomaticape<br><br>

<b>Descripcion:</b><br>
Analiza un vector clasificado (por ejemplo, el generado despues de una clasificacion supervisada).
Agrupa los poligonos por su campo de clase y suma sus areas.
Luego, genera un reporte estadistico y graficos.<br><br>

<b>Entradas:</b>
<ul>
<li><b>Vector clasificado:</b> Capa de poligonos.</li>
<li><b>Campo Nombre (NAME):</b> El campo de texto que contiene el nombre de la clase.</li>
<li><b>Campo Area (Area_Ha):</b> El campo numerico con el area en hectareas (o la unidad deseada).</li>
</ul>

<b>Salidas (en la carpeta seleccionada):</b>
<ul>
<li><b>reporte_estadistico.csv:</b> Tabla con la clase, area total y porcentaje.</li>
<li><b>grafico_barras.png:</b> Histograma de areas por clase.</li>
<li><b>grafico_pastel.png:</b> Diagrama de sectores por clase.</li>
</ul>
"""

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_VECTOR, self.tr("Vector clasificado (Poligonos)"),
            types=[QgsProcessing.SourceType.TypeVectorPolygon]
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_NAME, self.tr("Campo Clase / Nombre"),
            parentLayerParameterName=self.INPUT_VECTOR,
            defaultValue="name"
        ))

        self.addParameter(QgsProcessingParameterField(
            self.FIELD_AREA, self.tr("Campo Area (ej. Area_Ha)"),
            parentLayerParameterName=self.INPUT_VECTOR,
            defaultValue="area_ha"
        ))

        self.addParameter(QgsProcessingParameterFolderDestination(
            self.OUT_FOLDER, self.tr("Carpeta de salida")
        ))

    def processAlgorithm(self, parameters, context, feedback):
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import numpy as np
        except ImportError:
            raise QgsProcessingException(
                "No se encontro la libreria matplotlib. Instala matplotlib para generar graficos.")

        vl = self.parameterAsVectorLayer(
            parameters, self.INPUT_VECTOR, context)
        f_name = self.parameterAsString(parameters, self.FIELD_NAME, context)
        f_area = self.parameterAsString(parameters, self.FIELD_AREA, context)
        out_dir = self.parameterAsString(parameters, self.OUT_FOLDER, context)

        if vl is None:
            raise QgsProcessingException("Capa vectorial no valida.")

        os.makedirs(out_dir, exist_ok=True)

        feedback.pushInfo(
            "====================================================")
        feedback.pushInfo("Analizando areas por clase...")

        # Agrupar areas
        stats = {}
        for feat in vl.getFeatures():
            if feedback.isCanceled():
                break
            clase = feat[f_name]
            try:
                area = float(feat[f_area])
            except (ValueError, TypeError):
                area = 0.0

            clase_str = str(clase) if clase else "Desconocido"

            if clase_str not in stats:
                stats[clase_str] = 0.0
            stats[clase_str] += area

        if not stats:
            raise QgsProcessingException(
                "No se encontraron registros validos para agrupar.")

        # Calcular totales y porcentajes
        total_area = sum(stats.values())
        clases = list(stats.keys())
        areas = [stats[c] for c in clases]
        porcentajes = [(a / total_area) * 100 if total_area >
                       0 else 0 for a in areas]

        # Exportar CSV
        csv_path = os.path.join(out_dir, "reporte_estadistico.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Clase", "Area_Ha", "Porcentaje(%)"])
            for c, a, p in zip(clases, areas, porcentajes):
                writer.writerow([c, round(a, 4), round(p, 2)])
                feedback.pushInfo(f" - {c}: {a:.4f} Ha ({p:.2f}%)")

        feedback.pushInfo(f"Total Area: {total_area:.4f} Ha")
        feedback.pushInfo(f"CSV guardado en: {csv_path}")

        # Generar graficos
        colors = plt.cm.tab20(np.linspace(0, 1, len(clases)))

        # 1. Grafico de barras
        bar_path = os.path.join(out_dir, "grafico_barras.png")
        fig_bar, ax_bar = plt.subplots(figsize=(10, 6))
        bars = ax_bar.bar(clases, areas, color=colors)
        ax_bar.set_ylabel("Area (Ha)")
        ax_bar.set_title("Area Total por Clase")
        plt.xticks(rotation=45, ha='right')

        # Anotar barras
        for bar, area in zip(bars, areas):
            yval = bar.get_height()
            ax_bar.text(bar.get_x() + bar.get_width() / 2.0, yval, f'{area:.1f}',
                        va='bottom', ha='center', fontsize=9)

        fig_bar.tight_layout()
        fig_bar.savefig(bar_path, dpi=150)
        plt.close(fig_bar)

        # 2. Grafico circular (Pastel)
        pie_path = os.path.join(out_dir, "grafico_pastel.png")
        fig_pie, ax_pie = plt.subplots(figsize=(8, 8))

        # Filtrar clases con area == 0 para el pie chart
        pie_clases = [c for c, a in zip(clases, areas) if a > 0]
        pie_areas = [a for a in areas if a > 0]
        pie_colors = [colors[i] for i, a in enumerate(areas) if a > 0]

        if pie_areas:
            wedges, texts, autotexts = ax_pie.pie(pie_areas, labels=pie_clases, autopct='%1.1f%%',
                                                  startangle=140, colors=pie_colors)
            ax_pie.set_title("Distribucion Porcentual por Clase")
            fig_pie.tight_layout()
            fig_pie.savefig(pie_path, dpi=150)
        plt.close(fig_pie)

        feedback.pushInfo(f"Grafico de barras: {bar_path}")
        feedback.pushInfo(f"Grafico pastel: {pie_path}")
        feedback.pushInfo(
            "====================================================")
        feedback.setProgress(100)

        return {self.OUT_FOLDER: out_dir}

    def run(self):
        processing.execAlgorithmDialog(self)
