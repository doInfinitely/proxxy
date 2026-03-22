import {
  useState,
  useRef,
  useEffect,
  type FormEvent,
  type KeyboardEvent,
} from "react";
import Markdown from "react-markdown";
import type { ChatMessage } from "../hooks/useWebSocket";

interface ChatPanelProps {
  messages: ChatMessage[];
  status: "idle" | "thinking" | "browsing";
  onSend: (message: string) => void;
  onStop: () => void;
  onNewChat: () => void;
}

const SUGGESTIONS = [
  "Search Google for the latest AI news",
  "Go to Hacker News and summarize the top stories",
  "Find the weather forecast for New York City",
  "Look up the current price of Bitcoin",
];

function SendIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 2L11 13" />
      <path d="M22 2L15 22L11 13L2 9L22 2Z" />
    </svg>
  );
}

export default function ChatPanel({
  messages, status, onSend, onStop, onNewChat,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = input.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setInput("");
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleTextareaInput = () => {
    const el = textareaRef.current;
    if (el) {
      el.style.height = "auto";
      el.style.height = Math.min(el.scrollHeight, 120) + "px";
    }
  };

  const isWorking = status !== "idle";

  return (
    <div className="chat-panel">
      <div className="chat-header">
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <button className="new-chat-header-btn" onClick={onNewChat} title="New chat">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="12" y1="5" x2="12" y2="19" />
              <line x1="5" y1="12" x2="19" y2="12" />
            </svg>
          </button>
          <h1>Agent</h1>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12 }}>
          <span style={{ color: "var(--text-secondary)", opacity: 0.6 }}>
            {status === "idle" ? "Ready" : status === "thinking" ? "Thinking..." : "Browsing..."}
          </span>
          <div className={`status-dot ${status === "idle" ? "idle" : "active"}`} />
        </div>
      </div>

      <div className="chat-messages">
        {messages.length === 0 ? (
          <div className="welcome">
            <h2>What can I help with?</h2>
            <p>
              I'll browse the web to help you find information, research topics, and complete tasks.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  className="suggestion-btn"
                  onClick={() => onSend(s)}
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          messages.map((msg) => (
            <div key={msg.id} className={`message ${msg.role}`}>
              {msg.role === "user" || msg.role === "status" ? (
                msg.content
              ) : (
                <Markdown>{msg.content}</Markdown>
              )}
            </div>
          ))
        )}

        {isWorking && (
          <div className="typing-indicator">
            <span /><span /><span />
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      <div className="chat-input-area">
        <form className="chat-input-wrapper" onSubmit={handleSubmit}>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            onInput={handleTextareaInput}
            placeholder={isWorking ? "Agent is working..." : "Ask me anything..."}
            rows={1}
            disabled={isWorking}
          />
          {isWorking ? (
            <button
              type="button"
              className="send-btn"
              onClick={onStop}
              title="Stop agent"
              style={{ background: "#da1c1c" }}
            >
              <svg viewBox="0 0 24 24" fill="currentColor">
                <rect x="6" y="6" width="12" height="12" rx="2" />
              </svg>
            </button>
          ) : (
            <button
              type="submit"
              className="send-btn"
              disabled={!input.trim()}
              title="Send message"
            >
              <SendIcon />
            </button>
          )}
        </form>
      </div>
    </div>
  );
}
