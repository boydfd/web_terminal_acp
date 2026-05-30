import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { search } from "../api";

const sourceLabels: Record<string, string> = {
  virtual_window_id: "Window",
  title: "Title",
  tags: "Tags",
  folder_path: "Folder",
  provider: "Provider",
  kind: "Kind"
};

function formatSourceValue(value: string | string[] | null | undefined) {
  if (Array.isArray(value)) {
    return value.length > 0 ? value.join(", ") : "—";
  }
  return value ?? "—";
}

type SearchPanelProps = {
  clientId: string | null;
  onSelectWindowId?: (windowId: string) => void;
};

export function SearchPanel({ clientId, onSelectWindowId }: SearchPanelProps) {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const searchQuery = useQuery({
    queryKey: ["search", clientId, submitted],
    queryFn: () => search(clientId as string, submitted),
    enabled: clientId !== null && submitted.length > 0
  });
  const completedSearch = submitted.length > 0 && searchQuery.isSuccess;
  const resultCount = searchQuery.data?.results.length ?? 0;

  if (clientId === null) {
    return (
      <section className="search-panel" aria-labelledby="artifact-search-heading" data-onboarding-id="artifact-search">
        <h2 id="artifact-search-heading">Artifact search</h2>
        <p className="muted">Select a client to search artifacts.</p>
      </section>
    );
  }

  return (
    <section className="search-panel" aria-labelledby="artifact-search-heading" data-onboarding-id="artifact-search">
      <h2 id="artifact-search-heading">Artifact search</h2>
      <form
        className="search-form"
        onSubmit={(event) => {
          event.preventDefault();
          const trimmed = query.trim();
          if (!trimmed) {
            setSubmitted("");
            return;
          }
          if (trimmed === submitted) {
            void searchQuery.refetch();
            return;
          }
          setSubmitted(trimmed);
        }}
      >
        <label htmlFor="artifact-search-input">Search artifacts</label>
        <div className="search-form-row">
          <input
            id="artifact-search-input"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search artifacts..."
          />
          <button type="submit">Search</button>
        </div>
      </form>
      {searchQuery.isLoading && <p className="muted">Searching...</p>}
      {searchQuery.isError && (
        <p className="error" role="alert">
          Search failed.
        </p>
      )}
      {completedSearch && (
        <p className="muted">
          {resultCount === 0 ? "No results found." : `${resultCount} result${resultCount === 1 ? "" : "s"} found.`}
        </p>
      )}
      <div className="search-results">
        {searchQuery.data?.results.map((result) => {
          const sourceEntries = Object.entries(result.source);
          const windowId = result.source.virtual_window_id;
          return (
            <article key={`${result.index}:${result.id}`} className="search-result">
              <div className="search-result-header">
                <strong>{result.index}</strong>
                {result.score !== null && <span className="muted">Score {result.score.toFixed(2)}</span>}
                {windowId && onSelectWindowId && (
                  <button type="button" onClick={() => onSelectWindowId(windowId)}>
                    Open window
                  </button>
                )}
              </div>
              <p>{result.snippet || "No snippet available."}</p>
              {sourceEntries.length > 0 && (
                <dl>
                  {sourceEntries.map(([key, value]) => (
                    <div key={key}>
                      <dt>{sourceLabels[key] ?? key}</dt>
                      <dd>{formatSourceValue(value)}</dd>
                    </div>
                  ))}
                </dl>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
