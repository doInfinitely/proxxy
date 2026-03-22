import { useCallback, useEffect, useRef, useState } from "react";
import type { BrowserAction } from "../components/BrowserView";

export interface TabInfo {
  id: string;
  url: string;
  title: string;
  active: boolean;
}

export interface WsMessage {
  type: "screenshot" | "status" | "done" | "assistant" | "action" | "auth_required" | "url_update" | "tabs" | "cursor" | "clipboard";
  content?: string;
  data?: string;
  status?: string;
  url?: string;
  tabs?: TabInfo[];
  cursor?: string;
  text?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "status" | "done";
  content: string;
}

export function useWebSocket(url: string) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<"idle" | "thinking" | "browsing">("idle");
  const [screenshot, setScreenshot] = useState<string | null>(null);
  const [currentUrl, setCurrentUrl] = useState<string>("");
  const [tabs, setTabs] = useState<TabInfo[]>([]);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cursorStyle, setCursorStyle] = useState<string>("default");

  const addMessage = useCallback((role: ChatMessage["role"], content: string) => {
    setMessages((prev) => [
      ...prev,
      { id: `${Date.now()}-${Math.random()}`, role, content },
    ]);
  }, []);

  const connect = useCallback(() => {
    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => setConnected(true);
    ws.onclose = () => {
      setConnected(false);
      setStatus("idle");
      setTimeout(connect, 2000);
    };

    ws.onmessage = (event) => {
      const msg: WsMessage = JSON.parse(event.data);

      switch (msg.type) {
        case "screenshot":
          if (msg.data) setScreenshot(msg.data);
          break;
        case "tabs":
          if (msg.tabs) setTabs(msg.tabs);
          break;
        case "cursor":
          if (msg.cursor) setCursorStyle(msg.cursor);
          break;
        case "status":
          if (msg.status === "idle") setStatus("idle");
          else if (msg.status === "thinking") setStatus("thinking");
          else setStatus("browsing");

          if (msg.content?.startsWith("Browsing: ")) {
            setCurrentUrl(msg.content.replace("Browsing: ", ""));
            setStatus("browsing");
            addMessage("status", msg.content);
          }
          break;
        case "url_update":
          if (msg.url) setCurrentUrl(msg.url);
          break;
        case "clipboard":
          if (msg.text) navigator.clipboard.writeText(msg.text).catch(() => {});
          break;
        case "done":
          addMessage("done", msg.content || "Task completed.");
          setStatus("idle");
          break;
        case "assistant":
        case "action":
        case "auth_required":
          addMessage("assistant", msg.content || "");
          break;
      }
    };
  }, [url, addMessage]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
    };
  }, [connect]);

  const sendMessage = useCallback(
    (content: string) => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        addMessage("user", content);
        wsRef.current.send(JSON.stringify({ type: "message", content }));
        setStatus("thinking");
      }
    },
    [addMessage]
  );

  const stopAgent = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "stop" }));
    }
  }, []);

  const sendBrowserAction = useCallback((action: BrowserAction) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "browser_action", ...action }));
    }
  }, []);

  const switchTab = useCallback((targetId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "switch_tab", target_id: targetId }));
    }
  }, []);

  const closeTab = useCallback((targetId: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "close_tab", target_id: targetId }));
    }
  }, []);

  const queryCursor = useCallback((x: number, y: number) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: "cursor_query", x, y }));
    }
  }, []);

  const clearMessages = useCallback(() => {
    setMessages([]);
  }, []);

  return {
    connected,
    status,
    screenshot,
    currentUrl,
    tabs,
    messages,
    setMessages,
    cursorStyle,
    sendMessage,
    stopAgent,
    sendBrowserAction,
    switchTab,
    closeTab,
    queryCursor,
    clearMessages,
  };
}
