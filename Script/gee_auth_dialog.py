import traceback
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QMessageBox, QProgressBar
)
from qgis.PyQt.QtCore import Qt, QCoreApplication
from qgis.core import QgsTask, QgsApplication, QgsSettings, QgsMessageLog, Qgis


class GEEAuthTask(QgsTask):
    """
    Tarea en segundo plano para manejar la autenticación de GEE
    sin congelar la interfaz de QGIS.
    """

    def __init__(self, project_id,
                 description="Autenticando Google Earth Engine"):
        super().__init__(description, QgsTask.Flag.CanCancel)
        self.project_id = project_id
        self.exception_msg = None
        self.success = False

    def run(self):
        try:
            import ee
            # Auth_mode por defecto lanza el navegador web automáticamente
            ee.Authenticate()

            # Si se proporcionó un proyecto, intentamos inicializar para
            # comprobar
            kwargs = {}
            if self.project_id:
                kwargs['project'] = self.project_id

            ee.Initialize(**kwargs)
            self.success = True
            return True

        except Exception as e:
            self.exception_msg = str(e)
            QgsMessageLog.logMessage(
                f"Error GEE Auth: {
                    traceback.format_exc()}",
                "GeomaticaPe",
                Qgis.MessageLevel.Critical)
            return False

    def finished(self, result):
        if result and self.success:
            QgsMessageLog.logMessage(
                "GEE Autenticación Exitosa",
                "GeomaticaPe",
                Qgis.MessageLevel.Success)
            # Guardamos el proyecto en QgsSettings de forma global
            settings = QgsSettings()
            if self.project_id:
                settings.setValue('geomaticape/gee_project', self.project_id)
            else:
                settings.remove('geomaticape/gee_project')
        else:
            QgsMessageLog.logMessage(
                f"Fallo GEE Auth: {
                    self.exception_msg}",
                "GeomaticaPe",
                Qgis.MessageLevel.Critical)


class GEEAuthDialog(QDialog):
    """
    Diálogo para ingresar el ID de proyecto y lanzar la tarea de autenticación OAuth.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Configuración y Autenticación GEE"))
        self.resize(450, 200)
        self.setWindowFlags(
            self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)

        layout = QVBoxLayout()
        self.setLayout(layout)

        # Instrucciones
        lbl_info = QLabel(self.tr(
            "Configura tu cuenta de correo y tu ID de Proyecto de Google Cloud. "
            "Si es tu primera vez, haz clic en Autenticar para conceder permisos a la API."
        ))
        lbl_info.setWordWrap(True)
        layout.addWidget(lbl_info)

        settings = QgsSettings()
        current_email = settings.value('geomaticape/gee_email', '', type=str)
        current_project = settings.value(
            'geomaticape/gee_project', '', type=str)

        # Campo Correo
        h_layout_email = QHBoxLayout()
        lbl_email = QLabel(self.tr("Correo GMAIL:"))
        lbl_email.setFixedWidth(120)
        self.txt_email = QLineEdit()
        if current_email:
            self.txt_email.setText(current_email)
        self.txt_email.setPlaceholderText(self.tr("Ej: usuario@gmail.com"))
        h_layout_email.addWidget(lbl_email)
        h_layout_email.addWidget(self.txt_email)
        layout.addLayout(h_layout_email)

        # Campo ID Proyecto
        h_layout_proj = QHBoxLayout()
        lbl_project = QLabel(self.tr("ID del proyecto GEE:"))
        lbl_project.setFixedWidth(120)
        self.txt_project = QLineEdit()
        if current_project:
            self.txt_project.setText(current_project)
        self.txt_project.setPlaceholderText(self.tr("Ej: mi-proyecto-12345"))
        h_layout_proj.addWidget(lbl_project)
        h_layout_proj.addWidget(self.txt_project)
        layout.addLayout(h_layout_proj)

        # Barra de progreso indeterminada
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)  # Indeterminado
        self.progress.hide()
        layout.addWidget(self.progress)

        # Botones
        btn_layout = QVBoxLayout()
        self.btn_auth = QPushButton(
            self.tr("Autenticar con Google y verificar conexión"))
        self.btn_save = QPushButton(self.tr("Guardar configuración"))
        self.btn_cancel = QPushButton(self.tr("Cerrar"))

        btn_layout.addWidget(self.btn_auth)

        h_btn = QHBoxLayout()
        h_btn.addStretch()
        h_btn.addWidget(self.btn_cancel)
        h_btn.addWidget(self.btn_save)
        btn_layout.addLayout(h_btn)

        layout.addLayout(btn_layout)

        self.btn_auth.clicked.connect(self.start_auth)
        self.btn_save.clicked.connect(self.save_config)
        self.btn_cancel.clicked.connect(self.reject)

    def tr(self, message):
        return QCoreApplication.translate('GeomaticaPe', message)

    def save_config(self):
        settings = QgsSettings()
        email = self.txt_email.text().strip()
        project = self.txt_project.text().strip()

        if email:
            settings.setValue('geomaticape/gee_email', email)
        else:
            settings.remove('geomaticape/gee_email')

        if project:
            settings.setValue('geomaticape/gee_project', project)
        else:
            settings.remove('geomaticape/gee_project')

        QMessageBox.information(
            self,
            self.tr("Guardado"),
            self.tr("Configuración guardada exitosamente."))
        self.accept()

    def start_auth(self):
        try:
            pass
        except ImportError:
            QMessageBox.critical(
                self,
                self.tr("Error"),
                self.tr(
                    "No se encontró el módulo 'earthengine-api'. Instálalo primero.")
            )
            return

        project_id = self.txt_project.text().strip()

        if not project_id:
            QMessageBox.warning(
                self,
                self.tr("Atención"),
                self.tr("Debes ingresar un ID de Proyecto GEE (Google Cloud Project) válido para continuar. Si no tienes uno, créalo en Google Cloud Console.")
            )
            return

        # Configurar la tarea
        self.task = GEEAuthTask(project_id, self.tr(
            "Autenticando Google Earth Engine"))
        self.task.taskCompleted.connect(self.on_task_completed)
        self.task.taskTerminated.connect(self.on_task_failed)

        # Bloquear UI y mostrar progreso
        self.btn_auth.setEnabled(False)
        self.btn_save.setEnabled(False)
        self.txt_project.setEnabled(False)
        self.txt_email.setEnabled(False)
        self.progress.show()

        # Iniciar tarea
        QgsApplication.taskManager().addTask(self.task)

    def on_task_completed(self):
        self.btn_auth.setEnabled(True)
        self.txt_project.setEnabled(True)
        self.txt_email.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.progress.hide()

        # Guardar automáticamente la configuración si la autenticación fue
        # exitosa
        self.save_config()
        QMessageBox.information(
            self,
            self.tr("Éxito"),
            self.tr("Autenticación con Google Earth Engine completada correctamente.")
        )
        self.accept()

    def on_task_failed(self):
        self.btn_auth.setEnabled(True)
        self.txt_project.setEnabled(True)
        self.txt_email.setEnabled(True)
        self.btn_save.setEnabled(True)
        self.progress.hide()
        msg = self.task.exception_msg if self.task and hasattr(
            self.task, 'exception_msg') else "Error desconocido"
        QMessageBox.critical(
            self,
            self.tr("Fallo en Autenticación"),
            self.tr(f"La autenticación falló:\n\n{msg}")
        )
