"use client";

import { Task } from "@/types";
import { useReviewPatchExport } from "@/hooks/use-tasks";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { toast } from "sonner";
import { FileDiff, Loader2 } from "lucide-react";

interface ReviewPatchExportProps {
  task: Task;
}

// Owner control on a root task: download the accepted patches as one .patch
// file to apply with `git apply` on your own checkout. Subtasks have no export
// of their own — the root carries the whole series.
export function ReviewPatchExport({ task }: ReviewPatchExportProps) {
  const exportPatches = useReviewPatchExport(task.id);

  if (task.parent_task_id !== null) return null;

  const handleExport = async () => {
    try {
      const count = await exportPatches.mutateAsync();
      toast.success(
        `${count} reviewed patch${count === 1 ? "" : "es"} downloaded — apply with git apply`,
      );
    } catch (error) {
      toast.error(
        `Failed to export: ${error instanceof Error ? error.message : "Unknown error"}`,
      );
    }
  };

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          variant="outline"
          size="icon"
          aria-label="Export reviewed patches"
          disabled={exportPatches.isPending}
          onClick={handleExport}
        >
          {exportPatches.isPending ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <FileDiff className="h-4 w-4" />
          )}
        </Button>
      </TooltipTrigger>
      <TooltipContent>
        Download the reviewed changes as a .patch to apply on your own checkout.
      </TooltipContent>
    </Tooltip>
  );
}
