# Add File menu item and shortcut
from PySide6.QtWidgets import QMainWindow, QMenu, QFileDialog
from PySide6.QtGui import QAction

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.file_menu = self.menuBar().addMenu("File")
        open_folder_action = QAction("Open Folder...", self)
        open_folder_action.setShortcut("Ctrl+K,Ctrl+O")
        open_folder_action.triggered.connect(self.open_project_folder)
        self.file_menu.addAction(open_folder_action)
        self.active_workspace = None
        # Assuming explorer is initialized elsewhere and set as self.explorer

    def open_project_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if folder:
            self.set_active_workspace(folder)
            if hasattr(self, 'explorer') and self.explorer:
                self.explorer.update_folder(folder)

    def set_active_workspace(self, path):
        self.active_workspace = path
        # Propagate to agents, explorer, etc.
