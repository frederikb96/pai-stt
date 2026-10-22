/**
 * pai-stt GNOME Shell Extension
 *
 * Shows status indicator in the top panel for the pai-stt dictation daemon.
 * Subscribes to DBus signals for real-time state updates.
 *
 * States:
 * - idle: Mic icon, no label (hidden when "show-when-idle" is off)
 * - offline: Grey mic icon — the daemon is not on the bus
 * - recording: Red dot pulsing with the input level. A live take shows the transcription
 *   preview, a batch take its elapsed time and a scrolling level history
 * - transcribing: Yellow icon + elapsed seconds
 * - done / empty / partial / error: Result label, back to idle after 5 seconds
 *
 * Click opens a popup with the full text, copy and start/stop items, and the list of past
 * recordings, each of which can be copied or transcribed again.
 *
 * Also owns the clipboard on the daemon's behalf: the daemon calls Copy() over DBus and GNOME
 * Shell holds the text itself, so no external process or X11 transfer is involved.
 */

import Clutter from "gi://Clutter";
import GLib from "gi://GLib";
import Gio from "gi://Gio";
import GObject from "gi://GObject";
import Pango from "gi://Pango";
import St from "gi://St";

import { Extension } from "resource:///org/gnome/shell/extensions/extension.js";
import * as Main from "resource:///org/gnome/shell/ui/main.js";
import * as PanelMenu from "resource:///org/gnome/shell/ui/panelMenu.js";
import * as PopupMenu from "resource:///org/gnome/shell/ui/popupMenu.js";

// DBus configuration - must match daemon
const DBUS_NAME = "com.github.frederikb.PaiStt";
const DBUS_PATH = "/com/github/frederikb/PaiStt";
const DBUS_INTERFACE = "com.github.frederikb.PaiStt";

// DBus service exported by this extension - must match daemon's clipboard module
const SHELL_NAME = "com.github.frederikb.PaiStt.Shell";
const SHELL_PATH = "/com/github/frederikb/PaiStt/Shell";
const SHELL_IFACE_XML = `
<node>
  <interface name="${SHELL_NAME}">
    <method name="Copy">
      <arg type="s" direction="in" name="text"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
  </interface>
</node>`;

// Icon names for each state
const ICONS = {
  idle: "audio-input-microphone-symbolic",
  offline: "audio-input-microphone-symbolic",
  recording: "media-record-symbolic",
  transcribing: "emblem-synchronizing-symbolic",
  done: "emblem-ok-symbolic",
  empty: "dialog-information-symbolic",
  partial: "dialog-warning-symbolic",
  error: "dialog-error-symbolic",
};

// States that return to idle after a delay
const RESULT_STATES = {
  done: { label: "Copied!", seconds: 5 },
  empty: { label: "Nothing heard", seconds: 5 },
  partial: { label: "Partial!", seconds: 5 },
  error: { label: "Failed!", seconds: 5 },
};

// All possible state classes for cleanup
const STATE_CLASSES = [
  "idle",
  "offline",
  "recording",
  "transcribing",
  "done",
  "empty",
  "partial",
  "error",
  "no-signal",
];

// Input level (dBFS of each ~100 ms chunk) mapped onto 0..1 for the pulse and the level history.
// Speech close to a headset sits near the top; a room's noise floor near the bottom.
const LEVEL_FLOOR_DB = -60;
const LEVEL_CEIL_DB = -15;
// Below this for NO_SIGNAL_MS the input is treated as dead: a muted or wrong microphone.
const NO_SIGNAL_DB = -75;
const NO_SIGNAL_MS = 3000;
// One bar of the level history per chunk: bar width plus gap, in pixels.
const WAVE_STEP_PX = 3;

/**
 * DBus service letting the daemon hand text to the Shell's own clipboard.
 */
class ShellClipboardService {
  constructor() {
    this._impl = Gio.DBusExportedObject.wrapJSObject(SHELL_IFACE_XML, this);
    this._impl.export(Gio.DBus.session, SHELL_PATH);
    this._nameId = Gio.bus_own_name_on_connection(
      Gio.DBus.session,
      SHELL_NAME,
      Gio.BusNameOwnerFlags.NONE,
      null,
      null
    );
  }

  Copy(text) {
    St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, text);
    log(`[pai-stt] Clipboard set: ${text.length} chars`);
    return true;
  }

  destroy() {
    if (this._nameId) {
      Gio.bus_unown_name(this._nameId);
      this._nameId = 0;
    }
    this._impl.unexport();
    this._impl = null;
  }
}

/**
 * Wrapping label whose minimum height is its full wrapped height.
 *
 * St.Viewport (the base of St.BoxLayout) allocates its content MAX(minimum, visible) and scrolls
 * over that range only. A wrapping St.Label reports a one-line minimum, so inside a scroll view
 * it would be squeezed to the visible height and ellipsized instead of scrolled.
 */
const FullHeightLabel = GObject.registerClass(
  class FullHeightLabel extends St.Label {
    vfunc_get_preferred_height(forWidth) {
      const [, natural] = super.vfunc_get_preferred_height(forWidth);
      return [natural, natural];
    }
  }
);

/**
 * Menu item that switches the popup between the transcript and the recordings list.
 * It overrides activate() without emitting, so the menu stays open.
 */
const ViewToggleItem = GObject.registerClass(
  class ViewToggleItem extends PopupMenu.PopupBaseMenuItem {
    _init(onToggle) {
      super._init();
      this._onToggle = onToggle;
      this._label = new St.Label({
        text: "",
        x_expand: true,
        y_align: Clutter.ActorAlign.CENTER,
      });
      this.add_child(this._label);
      this._arrow = new St.Icon({
        icon_name: "pan-end-symbolic",
        style_class: "popup-menu-arrow",
      });
      this.add_child(this._arrow);
    }

    setView(showingList) {
      this._label.text = showingList ? "Back to transcript" : "Recent recordings";
      this._arrow.icon_name = showingList ? "pan-start-symbolic" : "pan-end-symbolic";
    }

    activate(_event) {
      this._onToggle();
    }
  }
);

function clamp01(value) {
  return Math.min(1, Math.max(0, value));
}

function formatDuration(ms) {
  const total = Math.round(ms / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

/**
 * A recording's start in the machine's local time zone: "Today 09:32", "Yesterday 18:05",
 * otherwise "Mon 20.10. 14:11".
 */
function formatStart(iso) {
  const start = GLib.DateTime.new_from_iso8601(iso, null);
  if (!start) {
    return iso;
  }
  const local = start.to_local();
  const today = GLib.DateTime.new_now_local();
  const yesterday = today.add_days(-1);
  const sameDay = (a, b) => a.format("%F") === b.format("%F");
  if (sameDay(local, today)) {
    return `Today ${local.format("%H:%M")}`;
  }
  if (sameDay(local, yesterday)) {
    return `Yesterday ${local.format("%H:%M")}`;
  }
  return local.format("%a %d.%m. %H:%M");
}

const PaiSttIndicator = GObject.registerClass(
  class PaiSttIndicator extends PanelMenu.Button {
    _init(settings) {
      super._init(0.0, "pai-stt Indicator", false);

      this._settings = settings;
      this._state = "offline";
      this._mode = "live";
      this._text = "";
      // Popup follows the newest text while the reader sits at the end; scrolling up releases it,
      // returning to the very end re-arms it.
      this._followEnd = true;
      this._showingList = false;
      this._resetTimeoutId = null;
      this._tickId = null;
      this._phaseStart = 0;
      this._level = 0;
      this._lastSignal = 0;
      this._noSignal = false;
      this._wave = [];
      this._dbusSignalIds = [];
      this._nameWatchId = null;

      // Container box for icon + label + level history
      this._box = new St.BoxLayout({
        style_class: "panel-status-menu-box",
      });
      this.add_child(this._box);

      this._icon = new St.Icon({
        icon_name: ICONS.idle,
        style_class: "system-status-icon",
      });
      this._icon.set_pivot_point(0.5, 0.5);
      this._box.add_child(this._icon);

      // Label for transcription preview. Ellipsizes from the START so the panel always shows
      // the most recent words (the end of the daemon's text) rather than the oldest.
      this._label = new St.Label({
        text: "",
        y_align: Clutter.ActorAlign.CENTER,
        style_class: "pai-stt-label",
      });
      this._label.clutter_text.set_line_wrap(false);
      this._label.clutter_text.ellipsize = Pango.EllipsizeMode.START;
      this._box.add_child(this._label);

      // Level history of a batch take, newest bar on the right
      this._waveArea = new St.DrawingArea({
        style_class: "pai-stt-wave",
        y_align: Clutter.ActorAlign.CENTER,
        visible: false,
      });
      this._waveArea.connect("repaint", (area) => this._drawWave(area));
      this._box.add_child(this._waveArea);

      this._applyWidthSetting();
      this._settingsChangedIds = [
        this._settings.connect("changed::label-max-width", () => this._applyWidthSetting()),
        this._settings.connect("changed::show-when-idle", () => this._applyVisibility()),
      ];

      this._buildMenu();
      this._updateState("offline", "", "live");
    }

    /**
     * Apply width setting to label and level history (only max-width is dynamic).
     */
    _applyWidthSetting() {
      const maxWidth = this._settings.get_int("label-max-width");
      this._label.set_style(`max-width: ${maxWidth}px;`);
      this._waveArea.set_width(maxWidth);
    }

    _setLabel(text) {
      this._label.set_text(text);
      this._label.visible = text.length > 0;
    }

    _applyVisibility() {
      const resting = this._state === "idle" || this._state === "offline";
      this.visible = !resting || this._settings.get_boolean("show-when-idle");
    }

    /**
     * Build the popup menu: transcript or recordings list, copy, start/stop.
     */
    _buildMenu() {
      // Full text display (scrollable)
      this._textItem = new PopupMenu.PopupBaseMenuItem({
        reactive: false,
        can_focus: false,
      });

      // ScrollView for long text (sizing in CSS)
      this._scrollView = new St.ScrollView({
        style_class: "pai-stt-scroll",
        hscrollbar_policy: St.PolicyType.NEVER,
        vscrollbar_policy: St.PolicyType.AUTOMATIC,
      });

      this._textLabel = new FullHeightLabel({
        text: "No transcription yet",
        style_class: "pai-stt-popup-text",
        x_expand: true,
      });
      this._textLabel.clutter_text.set_line_wrap(true);
      this._textLabel.clutter_text.set_line_wrap_mode(0); // WORD
      this._textLabel.clutter_text.set_selectable(true);

      // BoxLayout is the scrollable viewport and constrains the wrap width
      const textBox = new St.BoxLayout({
        vertical: true,
        x_expand: true,
        style: "max-width: 400px;",
      });
      textBox.add_child(this._textLabel);
      this._scrollView.set_child(textBox);
      this._textItem.add_child(this._scrollView);
      this.menu.addMenuItem(this._textItem);

      const vadjustment = this._scrollView.vadjustment;
      vadjustment.connect("notify::value", (adj) => {
        this._followEnd = adj.value >= adj.upper - adj.page_size - 1;
      });
      vadjustment.connect("notify::upper", (adj) => {
        if (this._followEnd) {
          adj.value = adj.upper - adj.page_size;
        }
      });

      // Recordings list (scrollable), in place of the transcript while shown
      this._listItem = new PopupMenu.PopupBaseMenuItem({
        reactive: false,
        can_focus: false,
      });
      this._listView = new St.ScrollView({
        style_class: "pai-stt-list",
        hscrollbar_policy: St.PolicyType.NEVER,
        vscrollbar_policy: St.PolicyType.AUTOMATIC,
      });
      this._listBox = new St.BoxLayout({ vertical: true, x_expand: true });
      this._listView.set_child(this._listBox);
      this._listItem.add_child(this._listView);
      this.menu.addMenuItem(this._listItem);

      this._viewToggle = new ViewToggleItem(() => this._setListView(!this._showingList));
      this.menu.addMenuItem(this._viewToggle);

      this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

      this._copyItem = new PopupMenu.PopupMenuItem("Copy to Clipboard");
      this._copyItem.connect("activate", () => this._copyToClipboard());
      this.menu.addMenuItem(this._copyItem);

      this._startLiveItem = new PopupMenu.PopupMenuItem("Start live recording");
      this._startLiveItem.connect("activate", () => this._command("start"));
      this.menu.addMenuItem(this._startLiveItem);

      this._startBatchItem = new PopupMenu.PopupMenuItem("Start batch recording");
      this._startBatchItem.connect("activate", () => this._command("start-batch"));
      this.menu.addMenuItem(this._startBatchItem);

      this._stopItem = new PopupMenu.PopupMenuItem("Stop recording");
      this._stopItem.connect("activate", () => this._command("stop"));
      this.menu.addMenuItem(this._stopItem);

      this._setListView(false);

      // Opening resyncs with the daemon and lands on the newest text
      this.menu.connect("open-state-changed", (_menu, isOpen) => {
        if (!isOpen) {
          return;
        }
        this._followEnd = true;
        this._refreshPopup();
        if (this._showingList) {
          this._loadRecordings();
        }
      });
    }

    _setListView(showingList) {
      this._showingList = showingList;
      this._textItem.visible = !showingList;
      this._listItem.visible = showingList;
      this._viewToggle.setView(showingList);
      if (showingList) {
        this._loadRecordings();
      }
    }

    _updateMenuItems() {
      const online = this._state !== "offline";
      const recording = this._state === "recording";
      this._copyItem.visible = online;
      this._startLiveItem.visible = online && !recording;
      this._startBatchItem.visible = online && !recording;
      this._stopItem.visible = recording;
      this._viewToggle.visible = online;
      if (!online && this._showingList) {
        this._setListView(false);
      }
    }

    /**
     * Call a daemon method; `onReply` gets the unpacked reply, or null when the call failed.
     */
    _call(methodName, params, replyType, timeoutMs, onReply) {
      Gio.DBus.session.call(
        DBUS_NAME,
        DBUS_PATH,
        DBUS_INTERFACE,
        methodName,
        params,
        new GLib.VariantType(replyType),
        Gio.DBusCallFlags.NONE,
        timeoutMs,
        null,
        (connection, result) => {
          let reply = null;
          try {
            reply = connection.call_finish(result).deepUnpack();
          } catch (e) {
            log(`[pai-stt] ${methodName} failed: ${e.message}`);
          }
          onReply(reply);
        }
      );
    }

    /**
     * Send one daemon command (the same set the CLI uses); a refusal shows in the panel.
     */
    _command(command) {
      // A stop waits for the backend's receipt, so the reply can take a while.
      this._call("Command", new GLib.Variant("(s)", [command]), "(s)", 120000, (reply) => {
        const answer = reply ? reply[0] : "ERROR daemon not reachable";
        if (!answer.startsWith("OK")) {
          log(`[pai-stt] ${command}: ${answer}`);
          this._flashLabel("Refused!");
        }
      });
    }

    /**
     * Ask the daemon to re-deliver its current transcription to the clipboard.
     * The daemon owns the payload format and verifies the copy.
     */
    _copyToClipboard() {
      this._call("CopyToClipboard", null, "(b)", 15000, (reply) => {
        this._flashLabel(reply && reply[0] ? "Copied!" : "Copy failed!");
      });
    }

    /**
     * Show a short status in the panel; a resting indicator returns to rest afterwards.
     */
    _flashLabel(text) {
      this._setLabel(text);
      this.show();
      if (this._state === "idle" || this._state === "offline") {
        this._scheduleReset(3, this._state);
      }
    }

    /**
     * Subscribe to the daemon's signals and follow it appearing on and leaving the bus.
     */
    connectDbus() {
      const subscribe = (signal, handler) =>
        Gio.DBus.session.signal_subscribe(
          DBUS_NAME,
          DBUS_INTERFACE,
          signal,
          DBUS_PATH,
          null,
          Gio.DBusSignalFlags.NONE,
          handler
        );
      this._dbusSignalIds = [
        subscribe("StateChanged", (...args) => this._onStateChanged(...args)),
        subscribe("Level", (...args) => this._onLevel(...args)),
      ];
      // A daemon restart or crash would otherwise leave a stale state on the panel.
      this._nameWatchId = Gio.bus_watch_name_on_connection(
        Gio.DBus.session,
        DBUS_NAME,
        Gio.BusNameWatcherFlags.NONE,
        () => this.fetchStatus(),
        () => this._updateState("offline", "", this._mode)
      );
      log("[pai-stt] DBus signal subscription active");
    }

    /**
     * Ask the daemon for its state, full text and mode.
     */
    _getStatus(onStatus) {
      this._call("GetStatus", null, "(sss)", 2000, (reply) => {
        if (reply) {
          onStatus(...reply);
        }
      });
    }

    /**
     * Sync indicator with the daemon (recording may already be active).
     */
    fetchStatus() {
      this._getStatus((state, text, mode) => {
        log(`[pai-stt] Status: ${state} (${mode})`);
        this._updateState(state, text, mode);
      });
    }

    /**
     * Show the daemon's full text in the popup.
     */
    _refreshPopup() {
      if (this._state === "offline") {
        this._textLabel.set_text("The pai-stt daemon is not running.");
        return;
      }
      this._getStatus((state, text, _mode) => {
        if (text.length > 0) {
          this._textLabel.set_text(text);
        } else if (state !== "recording" && state !== "transcribing") {
          this._textLabel.set_text("No transcription yet");
        }
      });
    }

    /**
     * Fill the recordings list from the daemon.
     */
    _loadRecordings() {
      this._call("ListRecordings", null, "(s)", 5000, (reply) => {
        this._listBox.destroy_all_children();
        if (!reply) {
          this._listBox.add_child(this._listNote("The daemon did not answer."));
          return;
        }
        let recordings = [];
        try {
          recordings = JSON.parse(reply[0]);
        } catch (e) {
          log(`[pai-stt] Unreadable recordings list: ${e.message}`);
        }
        if (recordings.length === 0) {
          this._listBox.add_child(this._listNote("No recordings yet."));
          return;
        }
        for (const rec of recordings) {
          this._listBox.add_child(this._recordingRow(rec));
        }
        this._listView.vadjustment.value = 0;
      });
    }

    _listNote(text) {
      return new St.Label({ text, style_class: "pai-stt-rec-note" });
    }

    /**
     * One recording: clicking the row copies its transcript, the button transcribes it again.
     */
    _recordingRow(rec) {
      const row = new St.BoxLayout({ style_class: "pai-stt-rec-row", x_expand: true });

      const details = [formatStart(rec.started_at), formatDuration(rec.duration_ms)];
      details.push(rec.mode === "batch" ? "Batch" : "Live");
      if (rec.recording) {
        details.push("recording…");
      } else if (rec.transcribing) {
        details.push("transcribing…");
      } else if (rec.transcript && !rec.complete) {
        details.push("partial");
      }
      const meta = new St.Label({ text: details.join(" · "), style_class: "pai-stt-rec-meta" });
      const preview = new St.Label({
        text: rec.transcript
          ? rec.transcript.replace(/\s+/g, " ")
          : "No transcript — transcribe it again",
        style_class: rec.transcript ? "pai-stt-rec-text" : "pai-stt-rec-text missing",
      });
      preview.clutter_text.set_line_wrap(false);
      preview.clutter_text.ellipsize = Pango.EllipsizeMode.END;
      const column = new St.BoxLayout({ vertical: true, x_expand: true });
      column.add_child(meta);
      column.add_child(preview);

      const copyButton = new St.Button({
        style_class: "pai-stt-rec-main",
        child: column,
        x_expand: true,
        can_focus: true,
        reactive: Boolean(rec.transcript) && !rec.recording,
        accessible_name: "Copy transcript",
      });
      copyButton.connect("clicked", () => {
        this.menu.close();
        this._call("CopyRecording", new GLib.Variant("(s)", [rec.id]), "(b)", 15000, (reply) =>
          this._flashLabel(reply && reply[0] ? "Copied!" : "Copy failed!")
        );
      });
      row.add_child(copyButton);

      const retryButton = new St.Button({
        style_class: "pai-stt-rec-action",
        child: new St.Icon({ icon_name: "view-refresh-symbolic", icon_size: 16 }),
        y_align: Clutter.ActorAlign.CENTER,
        can_focus: true,
        reactive: !rec.recording && !rec.transcribing && rec.duration_ms > 0,
        accessible_name: "Transcribe again",
      });
      retryButton.connect("clicked", () => {
        this.menu.close();
        this._call("Retranscribe", new GLib.Variant("(s)", [rec.id]), "(b)", 5000, (reply) => {
          if (!(reply && reply[0])) {
            this._flashLabel("Refused!");
          }
        });
      });
      row.add_child(retryButton);
      return row;
    }

    /**
     * Unsubscribe from DBus signals and stop watching the daemon's name.
     */
    disconnectDbus() {
      for (const id of this._dbusSignalIds) {
        Gio.DBus.session.signal_unsubscribe(id);
      }
      this._dbusSignalIds = [];
      if (this._nameWatchId !== null) {
        Gio.bus_unwatch_name(this._nameWatchId);
        this._nameWatchId = null;
      }
      log("[pai-stt] DBus signal subscription removed");
    }

    _onStateChanged(_connection, _sender, _path, _iface, _signal, params) {
      const [state, text, mode] = params.recursiveUnpack();
      this._updateState(state, text, mode);
    }

    /**
     * One captured chunk's level: drives the pulse, the dead-input hint and the history.
     */
    _onLevel(_connection, _sender, _path, _iface, _signal, params) {
      if (this._state !== "recording") {
        return;
      }
      const [db] = params.recursiveUnpack();
      const level = clamp01((db - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB));
      // Rises at once, falls off over a few chunks, so syllables read as pulses.
      this._level = Math.max(level, this._level * 0.6);
      this._icon.ease({
        scale_x: 0.8 + 0.45 * this._level,
        scale_y: 0.8 + 0.45 * this._level,
        opacity: 130 + Math.round(125 * this._level),
        duration: 100,
        mode: Clutter.AnimationMode.EASE_OUT_QUAD,
      });

      const now = GLib.get_monotonic_time() / 1000;
      if (db > NO_SIGNAL_DB) {
        this._lastSignal = now;
      }
      const noSignal = now - this._lastSignal > NO_SIGNAL_MS;
      if (noSignal !== this._noSignal) {
        this._noSignal = noSignal;
        if (noSignal) {
          this._box.add_style_class_name("no-signal");
        } else {
          this._box.remove_style_class_name("no-signal");
        }
        this._renderRecordingLabel();
      }

      if (this._mode === "batch") {
        this._wave.push(level);
        const keep = Math.ceil(this._waveArea.width / WAVE_STEP_PX) + 1;
        if (this._wave.length > keep) {
          this._wave.splice(0, this._wave.length - keep);
        }
        this._waveArea.queue_repaint();
      }
    }

    _drawWave(area) {
      const cr = area.get_context();
      const [width, height] = area.get_surface_size();
      const color = area.get_theme_node().get_foreground_color();
      const bars = Math.min(this._wave.length, Math.floor(width / WAVE_STEP_PX));
      for (let i = 0; i < bars; i++) {
        const level = this._wave[this._wave.length - 1 - i];
        const barHeight = Math.max(2, Math.round(level * height));
        const x = width - (i + 1) * WAVE_STEP_PX;
        cr.setSourceRGBA(
          color.red / 255,
          color.green / 255,
          color.blue / 255,
          0.35 + 0.65 * level
        );
        cr.rectangle(x, (height - barHeight) / 2, WAVE_STEP_PX - 1, barHeight);
        cr.fill();
      }
      cr.$dispose();
    }

    /**
     * The panel label while recording: the live text, or a batch take's elapsed time.
     */
    _renderRecordingLabel() {
      if (this._state !== "recording") {
        return;
      }
      if (this._mode === "batch") {
        const elapsed = GLib.get_monotonic_time() / 1000 - this._phaseStart;
        this._setLabel(this._noSignal ? "No input" : formatDuration(elapsed));
      } else if (this._text.length > 0) {
        // Clutter's own ellipsize (set to START above) decides what fits and keeps the tail
        this._setLabel(this._text);
      } else {
        this._setLabel(this._noSignal ? "No input" : "Recording...");
      }
    }

    _startTicking(onTick) {
      this._stopTicking();
      this._tickId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 1000, () => {
        onTick();
        return GLib.SOURCE_CONTINUE;
      });
    }

    _stopTicking() {
      if (this._tickId !== null) {
        GLib.source_remove(this._tickId);
        this._tickId = null;
      }
    }

    _resetPulse() {
      this._icon.remove_all_transitions();
      this._icon.set_scale(1, 1);
      this._icon.opacity = 255;
      this._level = 0;
    }

    /**
     * Update indicator based on state.
     */
    _updateState(state, text, mode) {
      this._clearResetTimeout();
      const previous = this._state;
      const entering = state !== previous;
      this._state = state;
      this._mode = mode;
      this._text = text;

      STATE_CLASSES.forEach((cls) => this._box.remove_style_class_name(cls));
      this._box.add_style_class_name(state);
      this._icon.set_icon_name(ICONS[state] || ICONS.idle);
      this._updateMenuItems();
      this._applyVisibility();

      if (state !== "recording") {
        this._resetPulse();
        this._noSignal = false;
        this._waveArea.visible = false;
      }
      if (entering) {
        this._stopTicking();
        this._phaseStart = GLib.get_monotonic_time() / 1000;
      }

      if (state === "idle" || state === "offline") {
        this._setLabel("");
      } else if (state === "recording") {
        if (entering) {
          this._lastSignal = this._phaseStart;
          this._wave = [];
          this._setListView(false);
          this._textLabel.set_text(
            mode === "batch"
              ? "Recording (batch) — the text arrives after stop."
              : "Recording..."
          );
          if (mode === "batch") {
            this._startTicking(() => this._renderRecordingLabel());
          }
        }
        this._waveArea.visible = mode === "batch";
        this._waveArea.queue_repaint();
        this._renderRecordingLabel();
      } else if (state === "transcribing") {
        const render = () => {
          const elapsed = GLib.get_monotonic_time() / 1000 - this._phaseStart;
          this._setLabel(`Transcribing… ${Math.floor(elapsed / 1000)}s`);
        };
        render();
        if (entering) {
          this._textLabel.set_text("Transcribing...");
          this._startTicking(render);
        }
      } else if (RESULT_STATES[state]) {
        const config = RESULT_STATES[state];
        this._setLabel(config.label);
        this._scheduleReset(config.seconds, state);
      }

      // The signal carries only the panel tail; an open popup wants the whole text
      if (this.menu.isOpen) {
        this._refreshPopup();
      }
    }

    /**
     * Return to the resting look after a delay if still in the given state.
     */
    _scheduleReset(seconds, requiredState) {
      this._clearResetTimeout();
      this._resetTimeoutId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, seconds, () => {
        this._resetTimeoutId = null;
        if (this._state === requiredState) {
          this._updateState(requiredState === "offline" ? "offline" : "idle", "", this._mode);
        }
        return GLib.SOURCE_REMOVE;
      });
    }

    _clearResetTimeout() {
      if (this._resetTimeoutId !== null) {
        GLib.source_remove(this._resetTimeoutId);
        this._resetTimeoutId = null;
      }
    }

    /**
     * Clean up on destroy.
     */
    destroy() {
      this._clearResetTimeout();
      this._stopTicking();
      this.disconnectDbus();
      this._icon.remove_all_transitions();

      for (const id of this._settingsChangedIds) {
        this._settings.disconnect(id);
      }
      this._settingsChangedIds = [];

      super.destroy();
    }
  }
);

export default class PaiSttExtension extends Extension {
  enable() {
    this._settings = this.getSettings();
    this._indicator = new PaiSttIndicator(this._settings);
    Main.panel.addToStatusArea(this.uuid, this._indicator);
    this._indicator.connectDbus();
    this._clipboardService = new ShellClipboardService();
    log("[pai-stt] Extension enabled");
  }

  disable() {
    if (this._clipboardService) {
      this._clipboardService.destroy();
      this._clipboardService = null;
    }
    if (this._indicator) {
      this._indicator.destroy();
      this._indicator = null;
    }
    this._settings = null;
    log("[pai-stt] Extension disabled");
  }
}
