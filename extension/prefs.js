/**
 * pai-stt Extension Preferences
 */

import Adw from "gi://Adw";
import Gio from "gi://Gio";
import Gtk from "gi://Gtk";

import {
  ExtensionPreferences,
} from "resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js";

export default class PaiSttPreferences extends ExtensionPreferences {
  fillPreferencesWindow(window) {
    const settings = this.getSettings();

    const page = new Adw.PreferencesPage({
      title: "pai-stt",
      icon_name: "audio-input-microphone-symbolic",
    });
    window.add(page);

    const appearanceGroup = new Adw.PreferencesGroup({
      title: "Appearance",
      description: "Configure how the indicator looks in the panel",
    });
    page.add(appearanceGroup);

    const widthRow = new Adw.SpinRow({
      title: "Label Width",
      subtitle: "Maximum width of the transcription preview (pixels)",
      adjustment: new Gtk.Adjustment({
        lower: 50,
        upper: 500,
        step_increment: 10,
        page_increment: 50,
        value: settings.get_int("label-max-width"),
      }),
    });
    appearanceGroup.add(widthRow);

    settings.bind(
      "label-max-width",
      widthRow,
      "value",
      Gio.SettingsBindFlags.DEFAULT
    );

    const idleRow = new Adw.SwitchRow({
      title: "Show When Idle",
      subtitle: "Keep the icon in the panel between recordings",
    });
    appearanceGroup.add(idleRow);
    settings.bind("show-when-idle", idleRow, "active", Gio.SettingsBindFlags.DEFAULT);

    const otherRow = new Adw.SwitchRow({
      title: "Show Other Recordings",
      subtitle: "Red microphone and input level while another app records",
    });
    appearanceGroup.add(otherRow);
    settings.bind("show-other-recordings", otherRow, "active", Gio.SettingsBindFlags.DEFAULT);

    const microphoneGroup = new Adw.PreferencesGroup({
      title: "Microphone",
      description: "Show a warning unless the default microphone's name contains this text",
    });
    page.add(microphoneGroup);

    const microphoneRow = new Adw.EntryRow({ title: "Expected Microphone" });
    microphoneGroup.add(microphoneRow);
    settings.bind(
      "expected-microphone",
      microphoneRow,
      "text",
      Gio.SettingsBindFlags.DEFAULT
    );
  }
}
