type GitPendingBadgeProps = {
  visible: boolean;
};

export function GitPendingBadge({ visible }: GitPendingBadgeProps) {
  if (!visible) {
    return null;
  }

  return (
    <span className="git-pending-badge" title="Uncommitted changes in git worktree" aria-label="Uncommitted git changes">
      G
    </span>
  );
}
