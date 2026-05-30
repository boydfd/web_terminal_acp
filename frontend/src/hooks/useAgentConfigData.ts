import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchAgentConfig, updateAgentConfigItem } from "../api";
import type { AgentConfig } from "../types";

type UseAgentConfigDataOptions = {
  clientId: string | null;
  windowId: string | null;
  enabled: boolean;
};

export function useAgentConfigData({ clientId, windowId, enabled }: UseAgentConfigDataOptions) {
  const queryClient = useQueryClient();
  const queryKey = ["agent-config", clientId, windowId];
  const query = useQuery({
    queryKey,
    queryFn: () => fetchAgentConfig(clientId as string, windowId as string),
    enabled: enabled && clientId !== null && windowId !== null,
    refetchInterval: 10000
  });

  const toggleMutation = useMutation({
    mutationFn: ({
      sectionId,
      itemId,
      nextEnabled
    }: {
      sectionId: string;
      itemId: string;
      nextEnabled: boolean;
    }) => updateAgentConfigItem(
      clientId as string,
      windowId as string,
      sectionId,
      itemId,
      nextEnabled
    ),
    onSuccess: (updated) => {
      queryClient.setQueryData<AgentConfig>(queryKey, updated);
      queryClient.invalidateQueries({ queryKey });
    }
  });

  return {
    config: query.data ?? null,
    isLoading: query.isLoading,
    isError: query.isError,
    isFetching: query.isFetching,
    toggleItem: (sectionId: string, itemId: string, nextEnabled: boolean) => {
      toggleMutation.mutate({ sectionId, itemId, nextEnabled });
    },
    pendingItemId: toggleMutation.variables?.itemId ?? null,
    isToggling: toggleMutation.isPending,
    toggleError: toggleMutation.isError
  };
}
