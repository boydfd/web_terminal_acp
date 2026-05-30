import type { WindowTitleHistory, WindowTitleHistoryItem } from "../types";

type Props = {
  history: WindowTitleHistory | null;
  isLoading?: boolean;
  isError?: boolean;
  isFetching?: boolean;
  onPreviousPage?: () => void;
  onNextPage?: () => void;
};

function formatDateTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function sourceLabel(source: string): string {
  switch (source) {
    case "initial":
      return "Initial";
    case "baseline":
      return "Baseline";
    case "summary":
      return "Summary";
    case "manual":
      return "Manual";
    default:
      return source;
  }
}

function pageLabel(history: WindowTitleHistory): string {
  if (history.total === 0) {
    return "0 title updates";
  }
  const start = history.offset + 1;
  const end = history.offset + history.items.length;
  return `${start}-${end} of ${history.total}`;
}

function TitleHistoryRow({ item }: { item: WindowTitleHistoryItem }) {
  return (
    <article className="title-history-item">
      <header>
        <div>
          <strong title={item.title}>{item.title}</strong>
          <span>{sourceLabel(item.source)}</span>
        </div>
        <time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time>
      </header>
      <p>{item.summary ?? "No summary at this point."}</p>
    </article>
  );
}

export function TitleHistoryViewer({
  history,
  isLoading = false,
  isError = false,
  isFetching = false,
  onPreviousPage,
  onNextPage
}: Props) {
  if (isLoading) {
    return <p className="muted">Loading title history...</p>;
  }
  if (isError) {
    return <p className="error" role="alert">Failed to load title history.</p>;
  }
  if (history === null || history.items.length === 0) {
    return <p className="muted">No title history captured yet.</p>;
  }

  return (
    <div className="title-history-viewer">
      <div className="agent-record-pagination">
        <span>{pageLabel(history)}{isFetching ? " · refreshing" : ""}</span>
        <div>
          <button
            type="button"
            disabled={history.offset === 0 || isFetching}
            onClick={onPreviousPage}
          >
            Previous
          </button>
          <button
            type="button"
            disabled={!history.has_more || isFetching}
            onClick={onNextPage}
          >
            Next
          </button>
        </div>
      </div>
      <div className="title-history-list">
        {history.items.map((item) => (
          <TitleHistoryRow key={item.id} item={item} />
        ))}
      </div>
    </div>
  );
}
