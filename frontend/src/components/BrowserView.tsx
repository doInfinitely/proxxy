import { useRef, useCallback, useState, type MouseEvent, type KeyboardEvent, type WheelEvent } from "react";
import type { TabInfo } from "../hooks/useWebSocket";

interface BrowserViewProps {
  screenshot: string | null;
  currentUrl: string;
  tabs: TabInfo[];
  status: "idle" | "thinking" | "browsing";
  cursorStyle: string;
  onBrowserAction: (action: BrowserAction) => void;
  onSwitchTab: (targetId: string) => void;
  onCloseTab: (targetId: string) => void;
  onCursorQuery: (x: number, y: number) => void;
}

export interface BrowserAction {
  action: "click" | "mousedown" | "mouseup" | "type" | "keydown" | "scroll" | "paste" | "copy" | "selectAll";
  x?: number;
  y?: number;
  text?: string;
  key?: string;
  deltaX?: number;
  deltaY?: number;
}

export default function BrowserView({
  screenshot, currentUrl, tabs, status, cursorStyle,
  onBrowserAction, onSwitchTab, onCloseTab, onCursorQuery,
}: BrowserViewProps) {
  const imgRef = useRef<HTMLImageElement>(null);
  const viewportRef = useRef<HTMLDivElement>(null);
  const [focused, setFocused] = useState(false);
  const lastCursorQuery = useRef(0);
  const mouseDownCoords = useRef<{ x: number; y: number } | null>(null);
  const mouseDownTime = useRef(0);

  const toPageCoords = useCallback((clientX: number, clientY: number): { x: number; y: number } | null => {
    const img = imgRef.current;
    if (!img) return null;

    const rect = img.getBoundingClientRect();
    const naturalW = img.naturalWidth;
    const naturalH = img.naturalHeight;
    if (!naturalW || !naturalH) return null;

    const scale = Math.min(rect.width / naturalW, rect.height / naturalH);
    const renderedW = naturalW * scale;
    const renderedH = naturalH * scale;
    const offsetX = (rect.width - renderedW) / 2;
    const offsetY = (rect.height - renderedH) / 2;

    const imgX = clientX - rect.left - offsetX;
    const imgY = clientY - rect.top - offsetY;

    if (imgX < 0 || imgY < 0 || imgX > renderedW || imgY > renderedH) return null;

    return {
      x: Math.round((imgX / renderedW) * naturalW),
      y: Math.round((imgY / renderedH) * naturalH),
    };
  }, []);

  const handleMouseDown = useCallback((e: MouseEvent) => {
    const coords = toPageCoords(e.clientX, e.clientY);
    if (coords) {
      mouseDownCoords.current = coords;
      mouseDownTime.current = Date.now();
      onBrowserAction({ action: "mousedown", ...coords });
    }
    viewportRef.current?.focus();
  }, [toPageCoords, onBrowserAction]);

  const handleMouseUp = useCallback((e: MouseEvent) => {
    const coords = toPageCoords(e.clientX, e.clientY);
    mouseDownCoords.current = null;

    if (coords) {
      onBrowserAction({ action: "mouseup", ...coords });
    }
  }, [toPageCoords, onBrowserAction]);

  const handleMouseMove = useCallback((e: MouseEvent) => {
    const now = Date.now();
    if (now - lastCursorQuery.current < 150) return;
    lastCursorQuery.current = now;

    const coords = toPageCoords(e.clientX, e.clientY);
    if (coords) {
      onCursorQuery(coords.x, coords.y);
    }
  }, [toPageCoords, onCursorQuery]);

  const handleKeyDown = useCallback((e: KeyboardEvent) => {
    if (["Control", "Shift", "Alt", "Meta"].includes(e.key)) return;

    const mod = e.ctrlKey || e.metaKey;

    if (mod && e.key === "v") {
      e.preventDefault();
      navigator.clipboard.readText().then((text) => {
        if (text) onBrowserAction({ action: "paste", text });
      }).catch(() => {});
      return;
    }

    if (mod && e.key === "c") {
      e.preventDefault();
      onBrowserAction({ action: "copy" });
      return;
    }

    if (mod && e.key === "a") {
      e.preventDefault();
      onBrowserAction({ action: "selectAll" });
      return;
    }

    e.preventDefault();

    if (e.key.length === 1 && !mod) {
      onBrowserAction({ action: "type", text: e.key });
    } else {
      onBrowserAction({ action: "keydown", key: e.key });
    }
  }, [onBrowserAction]);

  const handleScroll = useCallback((e: WheelEvent) => {
    e.preventDefault();
    const coords = toPageCoords(e.clientX, e.clientY);
    const boost = 3;
    onBrowserAction({
      action: "scroll",
      x: coords?.x ?? 0,
      y: coords?.y ?? 0,
      deltaX: Math.round(e.deltaX * boost),
      deltaY: Math.round(e.deltaY * boost),
    });
  }, [toPageCoords, onBrowserAction]);

  const activeTab = tabs.find((t) => t.active);
  const displayUrl = activeTab?.url || currentUrl;

  return (
    <div className="browser-panel">
      {tabs.length > 1 && (
        <div className="tab-bar">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`tab${tab.active ? " active" : ""}`}
              onClick={() => onSwitchTab(tab.id)}
              title={tab.url}
            >
              <span className="tab-title">{tab.title || "New Tab"}</span>
              <span
                className="tab-close"
                onClick={(e) => { e.stopPropagation(); onCloseTab(tab.id); }}
              >
                x
              </span>
            </button>
          ))}
        </div>
      )}
      <div className="browser-header">
        <span className="logo">Agent</span>
        <div className="url-bar">
          {displayUrl || "No page loaded"}
        </div>
        {status === "browsing" && (
          <span style={{ fontSize: 11, opacity: 0.6 }}>Browsing...</span>
        )}
      </div>
      <div
        ref={viewportRef}
        className={`browser-viewport${focused ? " focused" : ""}`}
        tabIndex={0}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={screenshot ? handleKeyDown : undefined}
        onWheel={screenshot ? handleScroll : undefined}
      >
        {screenshot ? (
          <img
            ref={imgRef}
            src={`data:image/jpeg;base64,${screenshot}`}
            alt="Browser view"
            onMouseDown={handleMouseDown}
            onMouseUp={handleMouseUp}
            onMouseMove={handleMouseMove}
            draggable={false}
            style={{ cursor: cursorStyle }}
          />
        ) : (
          <div className="placeholder">
            <div className="icon">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10" />
                <path d="M2 12h20" />
                <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
              </svg>
            </div>
            <div>
              Send a message to start browsing.
              <br />
              The agent's browser view will appear here.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
