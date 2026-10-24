"use client";

import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import {
  ArrowUp,
  Check,
  Circle,
  FileText,
  LoaderCircle,
  Search,
  Table2,
} from "lucide-react";
import { snapshotAuthorizationHeaders } from "./live-provider";
import "./search-chat.css";

type Step = { label: string; detail?: string; done: boolean };
type Source = {
  ref: string;
  table: string;
  title: string;
  case_id: string | null;
  snippet: string;
  score: number;
};
type Metrics = {
  embeddedCount: number;
  queryEmbedded: boolean;
  openaiLatencyMs: number;
};
type Message =
  | { id: string; role: "user"; content: string }
  | {
      id: string;
      role: "assistant";
      content: string;
      steps: Step[];
      sources: Source[];
      status: "streaming" | "done" | "error";
      error?: string;
      metrics?: Metrics;
      startedAt: number;
    };
type SearchChatProps = { onSelectCase: (caseId: string) => void };

const examples = [
  "Which invoices are on hold and why?",
  "Show the bank detail change approval",
  "What did the Validator flag?",
];

function elapsedLabel(startedAt: number, now: number): string {
  const seconds = Math.max(0, (now - startedAt) / 1000);
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  return `${Math.floor(seconds / 60)}m ${(seconds % 60).toFixed(1)}s`;
}

function LoadingState({
  steps,
  startedAt,
}: {
  steps: Step[];
  startedAt: number;
}) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(timer);
  }, []);
  const current = steps.findIndex((step) => !step.done);

  return (
    <div className="search-chat-loading" role="status" aria-live="polite">
      <div className="search-chat-loading-heading">
        <span
          className="search-chat-pixel-grid grid shrink-0 grid-cols-[repeat(3,4px)] gap-[1.5px]"
          aria-hidden="true"
        >
          {Array.from({ length: 9 }, (_, index) => {
            const row = Math.floor(index / 3);
            const column = index % 3;
            return (
              <span
                key={index}
                className="size-[4px] bg-ink"
                style={{
                  animationDelay: `${(column + Math.abs(row - 1)) * 90}ms`,
                }}
              />
            );
          })}
        </span>
        <span className="search-chat-loading-label">
          Searching your records
        </span>
        <span className="search-chat-elapsed">
          {elapsedLabel(startedAt, now)}
        </span>
      </div>
      {steps.length > 0 && (
        <ol className="search-chat-steps">
          {steps.map((step, index) => {
            const isCurrent = index === current;
            return (
              <li
                key={`${step.label}-${index}`}
                className={step.done ? "done" : ""}
              >
                <span className="search-chat-step-icon">
                  {step.done ? (
                    <Check size={13} />
                  ) : isCurrent ? (
                    <LoaderCircle size={14} className="search-chat-spinner" />
                  ) : (
                    <Circle size={12} />
                  )}
                </span>
                <span className="search-chat-step-copy">
                  <span>{step.label}</span>
                  {step.detail && <small>{step.detail}</small>}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}

function SourceIcon({ table }: { table: string }) {
  return table === "invoice" || table === "doc_read" ? (
    <FileText size={14} aria-hidden="true" />
  ) : (
    <Table2 size={14} aria-hidden="true" />
  );
}

function SourceChips({
  sources,
  onSelectCase,
}: {
  sources: Source[];
  onSelectCase: (caseId: string) => void;
}) {
  if (!sources.length) return null;
  return (
    <div className="search-chat-sources">
      <div className="search-chat-sources-heading">Sources</div>
      <div className="search-chat-source-list">
        {sources.map((source) => (
          <button
            type="button"
            key={source.ref}
            className="search-chat-source-chip"
            title={
              source.case_id
                ? `${source.ref} · ${source.snippet}`
                : `${source.ref} · Shared record not attached to a case`
            }
            disabled={!source.case_id}
            onClick={() => source.case_id && onSelectCase(source.case_id)}
          >
            <SourceIcon table={source.table} />
            <span className="search-chat-source-title">{source.title}</span>
            <span className="search-chat-source-case">
              {source.case_id ?? "Shared"}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

function AnswerText({
  content,
  sources,
  onSelectCase,
}: {
  content: string;
  sources: Source[];
  onSelectCase: (caseId: string) => void;
}) {
  if (!content) return null;
  const sourceByRef = new Map(sources.map((source) => [source.ref, source]));
  const citation = /\[([a-z_]+:[^\]\s]+)\]/g;
  const nodes: ReactNode[] = [];
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = citation.exec(content))) {
    if (match.index > cursor)
      nodes.push(
        <span key={`text-${cursor}`}>
          {content.slice(cursor, match.index)}
        </span>,
      );
    const ref = match[1];
    const source = sourceByRef.get(ref);
    nodes.push(
      source ? (
        <button
          type="button"
          key={`citation-${match.index}`}
          className="search-chat-citation"
          disabled={!source.case_id}
          title={
            source.case_id
              ? `Open ${source.case_id} · ${source.title}`
              : `${source.ref} is not attached to a case`
          }
          onClick={() => source.case_id && onSelectCase(source.case_id)}
        >
          <SourceIcon table={source.table} />
          {ref}
        </button>
      ) : (
        <span
          className="search-chat-unmatched-citation"
          key={`citation-${match.index}`}
        >
          {match[0]}
        </span>
      ),
    );
    cursor = match.index + match[0].length;
  }
  if (cursor < content.length)
    nodes.push(<span key={`text-${cursor}`}>{content.slice(cursor)}</span>);
  return <div className="search-chat-answer">{nodes}</div>;
}

function Composer({
  value,
  disabled,
  onChange,
  onSubmit,
}: {
  value: string;
  disabled: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
}) {
  const textarea = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 160)}px`;
  }, [value]);
  return (
    <form className="search-chat-composer" onSubmit={onSubmit}>
      <textarea
        ref={textarea}
        aria-label="Ask about your records"
        placeholder="Ask about invoices, approvals, calls, or documents…"
        rows={1}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey) {
            event.preventDefault();
            event.currentTarget.form?.requestSubmit();
          }
        }}
      />
      <button
        type="submit"
        aria-label="Search records"
        disabled={disabled || !value.trim()}
      >
        <ArrowUp size={17} />
      </button>
    </form>
  );
}

function updateAssistant(
  messages: Message[],
  id: string,
  update: (message: Extract<Message, { role: "assistant" }>) => Message,
): Message[] {
  return messages.map((message) =>
    message.id === id && message.role === "assistant"
      ? update(message)
      : message,
  );
}

export default function SearchChat({ onSelectCase }: SearchChatProps) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const abort = useRef<AbortController | null>(null);

  useEffect(() => {
    const element = scroll.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [messages]);

  useEffect(
    () => () => {
      abort.current?.abort();
    },
    [],
  );

  const ask = async (rawQuestion: string) => {
    const question = rawQuestion.trim();
    if (!question || abort.current) return;
    const history = messages.flatMap((message) =>
      message.content ? [{ role: message.role, content: message.content }] : [],
    );
    const userId = crypto.randomUUID();
    const assistantId = crypto.randomUUID();
    const startedAt = Date.now();
    setInput("");
    setSending(true);
    setMessages((current) => [
      ...current,
      { id: userId, role: "user", content: question },
      {
        id: assistantId,
        role: "assistant",
        content: "",
        steps: [],
        sources: [],
        status: "streaming",
        startedAt,
      },
    ]);

    const controller = new AbortController();
    abort.current = controller;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...snapshotAuthorizationHeaders(),
        },
        body: JSON.stringify({ question, history }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const body = (await response.json().catch(() => null)) as {
          error?: string;
        } | null;
        throw new Error(
          body?.error ?? `Search returned HTTP ${response.status}`,
        );
      }
      if (!response.body) throw new Error("Search stream was unavailable.");
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      const handleEvent = (raw: string) => {
        if (!raw.trim()) return;
        const event = JSON.parse(raw) as
          | { type: "step"; label: string; detail?: string }
          | { type: "sources"; items: Source[] }
          | { type: "token"; text: string }
          | ({ type: "done" } & Metrics)
          | { type: "error"; message: string };
        if (event.type === "step") {
          setMessages((current) =>
            updateAssistant(current, assistantId, (message) => ({
              ...message,
              steps: [
                ...message.steps.map((step) => ({ ...step, done: true })),
                { label: event.label, detail: event.detail, done: false },
              ],
            })),
          );
        } else if (event.type === "sources") {
          setMessages((current) =>
            updateAssistant(current, assistantId, (message) => ({
              ...message,
              sources: event.items,
            })),
          );
        } else if (event.type === "token") {
          setMessages((current) =>
            updateAssistant(current, assistantId, (message) => ({
              ...message,
              content: message.content + event.text,
            })),
          );
        } else if (event.type === "done") {
          setMessages((current) =>
            updateAssistant(current, assistantId, (message) => ({
              ...message,
              status: "done",
              steps: message.steps.map((step) => ({ ...step, done: true })),
              metrics: {
                embeddedCount: event.embeddedCount,
                queryEmbedded: event.queryEmbedded,
                openaiLatencyMs: event.openaiLatencyMs,
              },
            })),
          );
        } else if (event.type === "error") {
          setMessages((current) =>
            updateAssistant(current, assistantId, (message) => ({
              ...message,
              status: "error",
              error: event.message,
            })),
          );
        }
      };
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) handleEvent(line);
      }
      if (buffer.trim()) handleEvent(buffer);
      setMessages((current) =>
        updateAssistant(current, assistantId, (message) =>
          message.status === "streaming"
            ? {
                ...message,
                status: "done",
                steps: message.steps.map((step) => ({ ...step, done: true })),
              }
            : message,
        ),
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      setMessages((current) =>
        updateAssistant(current, assistantId, (message) => ({
          ...message,
          status: "error",
          error:
            error instanceof Error
              ? error.message
              : "Search could not be completed.",
        })),
      );
    } finally {
      if (abort.current === controller) abort.current = null;
      setSending(false);
    }
  };

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    void ask(input);
  };

  return (
    <section
      className={`search-chat ${messages.length ? "has-messages" : "is-empty"}`}
      aria-label="Search operational records"
    >
      {messages.length === 0 ? (
        <div className="search-chat-empty-content">
          <div className="search-chat-welcome">
            <span className="search-chat-mark">
              <Search size={19} />
            </span>
            <h1>What would you like to find?</h1>
            <p>
              Search invoices, approvals, calls, documents, and the decisions
              behind each case.
            </p>
          </div>
          <div className="search-chat-composer-wrap">
            <Composer
              value={input}
              disabled={sending}
              onChange={setInput}
              onSubmit={submit}
            />
          </div>
          <div className="search-chat-examples" aria-label="Example questions">
            <span>Try asking</span>
            {examples.map((example) => (
              <button
                type="button"
                key={example}
                disabled={sending}
                onClick={() => void ask(example)}
              >
                {example}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <>
          <div className="search-chat-thread" ref={scroll}>
            <div className="search-chat-thread-inner">
              {messages.map((message) =>
                message.role === "user" ? (
                  <div className="search-chat-user-message" key={message.id}>
                    {message.content}
                  </div>
                ) : (
                  <article
                    className="search-chat-assistant-message"
                    key={message.id}
                    data-embedded-count={message.metrics?.embeddedCount}
                    data-query-embedded={message.metrics?.queryEmbedded}
                    data-openai-latency-ms={message.metrics?.openaiLatencyMs}
                  >
                    {message.status === "streaming" && (
                      <LoadingState
                        steps={message.steps}
                        startedAt={message.startedAt}
                      />
                    )}
                    <SourceChips
                      sources={message.sources}
                      onSelectCase={onSelectCase}
                    />
                    <AnswerText
                      content={message.content}
                      sources={message.sources}
                      onSelectCase={onSelectCase}
                    />
                    {message.error && (
                      <p className="search-chat-error" role="alert">
                        {message.error}
                      </p>
                    )}
                  </article>
                ),
              )}
            </div>
          </div>
          <div className="search-chat-composer-dock">
            <div className="search-chat-composer-wrap">
              <Composer
                value={input}
                disabled={sending}
                onChange={setInput}
                onSubmit={submit}
              />
              <p>
                Answers use the available case records and link back to their
                sources.
              </p>
            </div>
          </div>
        </>
      )}
    </section>
  );
}
