import BrowserView from "./components/BrowserView";
import ChatPanel from "./components/ChatPanel";
import { useWebSocket } from "./hooks/useWebSocket";

const WS_URL =
  import.meta.env.VITE_WS_URL ||
  `${window.location.protocol === "https:" ? "wss:" : "ws:"}//${window.location.host}/ws`;

export default function App() {
  const {
    status, screenshot, currentUrl, tabs, messages, cursorStyle,
    sendMessage, stopAgent, sendBrowserAction, switchTab, closeTab, queryCursor,
    clearMessages,
  } = useWebSocket(WS_URL);

  return (
    <div className="app">
      <BrowserView
        screenshot={screenshot}
        currentUrl={currentUrl}
        tabs={tabs}
        status={status}
        cursorStyle={cursorStyle}
        onBrowserAction={sendBrowserAction}
        onSwitchTab={switchTab}
        onCloseTab={closeTab}
        onCursorQuery={queryCursor}
      />
      <ChatPanel
        messages={messages}
        status={status}
        onSend={sendMessage}
        onStop={stopAgent}
        onNewChat={clearMessages}
      />
    </div>
  );
}
