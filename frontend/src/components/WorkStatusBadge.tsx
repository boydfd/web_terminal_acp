import type { WorkStatus } from "../types";

type WorkStatusBadgeProps = {
  status: WorkStatus;
};

function formatDateTime(value: string | null | undefined): string {
  if (value == null) {
    return "暂无活动记录";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

export function WorkStatusBadge({ status }: WorkStatusBadgeProps) {
  const className = `work-status-badge ${status.color}`;
  const title = `最近活动: ${formatDateTime(status.last_activity_at)}`;

  return (
    <span className={className} title={title}>
      <span aria-hidden="true" />
      {status.label}
    </span>
  );
}

export function WorkStatusDot({ status }: WorkStatusBadgeProps) {
  const className = `work-status-dot ${status.color}`;
  const title = `${status.label} · 最近活动: ${formatDateTime(status.last_activity_at)}`;

  return <span className={className} title={title} aria-label={status.label} />;
}
