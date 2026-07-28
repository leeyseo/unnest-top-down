export type shortcutsStoreType = {
  updateUniqueShortcut: (name: string, combination: string) => void;
  outputInspection: string;
  play: string;
  flow: string;
  group: string;
  cut: string;
  paste: string;
  api: string;
  openPlayground: string;
  undo: string;
  redo: string;
  redoAlt: string;
  advancedSettings: string;
  minimize: string;
  code: string;
  copy: string;
  duplicate: string;
  searchComponentsSidebar: string;
  changesSave: string;
  saveComponent: string;
  delete: string;
  update: string;
  download: string;
  toggleSidebar: string;
  freezePath: string;
  toolMode: string;
  aiAssistant: string;
  shortcuts: Array<{
    name: string;
    display_name: string;
    shortcut: string;
  }>;
  setShortcuts: (
    newShortcuts: Array<{
      name: string;
      display_name: string;
      shortcut: string;
    }>,
  ) => void;
  getShortcutsFromStorage: () => void;
};
