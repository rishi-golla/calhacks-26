"use client";

import { forwardRef, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  House,
  MessagesSquare,
  PanelLeftClose,
  PanelLeftOpen,
  Search,
  SquarePen,
  X,
} from "lucide-react";
import type { Row } from "@/lib/data";
import GlideMenu from "./primitives/GlideMenu";
import "./sidebar-nav.css";

type SidebarNavProps = {
  cases: Row[];
  caseId: string;
  currentView: string;
  collapsed: boolean;
  narrow: boolean;
  mobileOpen: boolean;
  onToggleCollapsed: () => void;
  onNewChat: () => void;
  onHome: () => void;
  onSelectCase: (caseId: string) => void;
  onCloseMobile: () => void;
};

const SIDEBAR_WIDTH = 224;
const COLLAPSED_WIDTH = 52;
const MOBILE_WIDTH = 240;

const SidebarNav = forwardRef<HTMLElement, SidebarNavProps>(function SidebarNav(
  {
    cases,
    caseId,
    currentView,
    collapsed,
    narrow,
    mobileOpen,
    onToggleCollapsed,
    onNewChat,
    onHome,
    onSelectCase,
    onCloseMobile,
  },
  ref,
) {
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [chatsOpen, setChatsOpen] = useState(true);
  const searchRef = useRef<HTMLInputElement>(null);
  const isCollapsed = collapsed && !narrow;
  const width = narrow
    ? MOBILE_WIDTH
    : isCollapsed
      ? COLLAPSED_WIDTH
      : SIDEBAR_WIDTH;

  useEffect(() => {
    if (searchOpen) searchRef.current?.focus();
  }, [searchOpen]);

  useEffect(() => {
    if (!isCollapsed) return;
    setSearchOpen(false);
    setQuery("");
  }, [isCollapsed]);

  const visibleCases = useMemo(() => {
    const search = query.trim().toLowerCase();
    return cases.filter((row) => {
      const id = String(row.case_id ?? "");
      if (!id) return false;
      const label = String(row.title || id).replace(/^\[W2 fixture\]\s*/, "");
      return !search || `${label} ${id}`.toLowerCase().includes(search);
    });
  }, [cases, query]);

  const openSearch = () => {
    if (isCollapsed) onToggleCollapsed();
    setChatsOpen(true);
    setSearchOpen(true);
  };

  const closeSearch = () => {
    setSearchOpen(false);
    setQuery("");
  };

  return (
    <aside
      ref={ref}
      aria-label="Golden Gate Freight navigation"
      aria-hidden={narrow && !mobileOpen}
      inert={narrow && !mobileOpen}
      className={`sidebar sidebar-nav ${mobileOpen ? "open" : ""}`}
      data-sidebar-collapsed={isCollapsed}
      style={{ width }}
    >
      <div className="sidebar-nav-header">
        <div className="sidebar-nav-brand">
          <span className="sidebar-nav-brand-dot" aria-hidden="true" />
          <span className="sidebar-nav-copy">Golden Gate Freight</span>
        </div>
        <button
          type="button"
          className="sidebar-nav-collapse"
          aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={onToggleCollapsed}
        >
          {isCollapsed ? (
            <PanelLeftOpen size={17} />
          ) : (
            <PanelLeftClose size={17} />
          )}
        </button>
        <button
          type="button"
          className="sidebar-nav-mobile-close"
          aria-label="Close navigation"
          onClick={onCloseMobile}
        >
          <X size={18} />
        </button>
      </div>

      <GlideMenu
        className="sidebar-nav-actions"
        highlightClassName="sidebar-nav-glide-highlight"
      >
        <button
          type="button"
          className="sidebar-nav-row nav-item"
          data-glide-row
          title="New chat"
          onClick={onNewChat}
        >
          <SquarePen size={18} aria-hidden="true" />
          <span className="sidebar-nav-copy">New chat</span>
        </button>
        <button
          type="button"
          className={`sidebar-nav-row nav-item ${currentView === "home" ? "active" : ""}`}
          data-glide-row
          data-active={currentView === "home"}
          aria-current={currentView === "home" ? "page" : undefined}
          title="Home"
          onClick={onHome}
        >
          <House size={18} aria-hidden="true" />
          <span className="sidebar-nav-copy">Home</span>
        </button>
      </GlideMenu>

      <section className="sidebar-nav-chat-section">
        <div className="sidebar-nav-chat-header">
          <button
            type="button"
            className="sidebar-nav-section-toggle"
            aria-expanded={chatsOpen}
            aria-label={chatsOpen ? "Collapse chats" : "Expand chats"}
            title={chatsOpen ? "Collapse chats" : "Expand chats"}
            onClick={() => setChatsOpen((open) => !open)}
          >
            <ChevronDown
              size={16}
              aria-hidden="true"
              className={chatsOpen ? "" : "is-collapsed"}
            />
            <span className="sidebar-nav-copy">Chats</span>
          </button>
          <button
            type="button"
            className={`sidebar-nav-search-button ${searchOpen ? "is-hidden" : ""}`}
            aria-label="Search chats"
            aria-expanded={searchOpen}
            title="Search chats"
            onClick={openSearch}
          >
            <Search size={16} aria-hidden="true" />
          </button>
          <div
            className={`sidebar-nav-search ${searchOpen ? "is-open" : ""}`}
            aria-hidden={!searchOpen}
          >
            <Search size={15} aria-hidden="true" />
            <input
              ref={searchRef}
              aria-label="Search chats"
              autoComplete="off"
              tabIndex={searchOpen ? 0 : -1}
              placeholder="Search chats"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Escape") closeSearch();
              }}
            />
            <button
              type="button"
              aria-label="Close chat search"
              tabIndex={searchOpen ? 0 : -1}
              onClick={closeSearch}
            >
              <X size={15} aria-hidden="true" />
            </button>
          </div>
        </div>
        <div
          className="sidebar-nav-case-scroll"
          hidden={!chatsOpen}
          aria-label="Chats"
        >
          <GlideMenu
            className="sidebar-nav-case-menu"
            highlightClassName="sidebar-nav-glide-highlight"
          >
            {visibleCases.map((row) => {
              const id = String(row.case_id);
              const label = String(row.title || id).replace(
                /^\[W2 fixture\]\s*/,
                "",
              );
              const active = caseId === id;
              return (
                <button
                  key={id}
                  type="button"
                  className={`sidebar-nav-row sidebar-nav-case ${active ? "active" : ""}`}
                  data-glide-row
                  data-active={active}
                  aria-current={active ? "page" : undefined}
                  title={`${label} · ${id}`}
                  onClick={() => onSelectCase(id)}
                >
                  <MessagesSquare
                    className="sidebar-nav-case-icon"
                    size={16}
                    aria-hidden="true"
                  />
                  <span className="sidebar-nav-copy">{label}</span>
                </button>
              );
            })}
            {visibleCases.length === 0 && (
              <p className="sidebar-nav-empty">
                {query ? "No chats found" : "No chats yet"}
              </p>
            )}
          </GlideMenu>
        </div>
      </section>
    </aside>
  );
});

SidebarNav.displayName = "SidebarNav";

export default SidebarNav;
