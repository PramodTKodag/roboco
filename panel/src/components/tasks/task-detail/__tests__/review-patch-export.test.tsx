import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TaskStatus, Team, TaskType, type Task } from "@/types";

const tasksApi = vi.hoisted(() => ({ getReviewExport: vi.fn() }));
vi.mock("@/lib/api/tasks", () => ({ tasksApi }));
vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

let lastBlob: Blob | null = null;
const anchors: HTMLAnchorElement[] = [];

beforeAll(() => {
  URL.createObjectURL = vi.fn((blob: Blob) => {
    lastBlob = blob;
    return "blob:mock-url";
  }) as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = vi.fn();
  const realCreateElement = document.createElement.bind(document);
  vi.spyOn(document, "createElement").mockImplementation(
    (tag: string, options?: ElementCreationOptions) => {
      const el = realCreateElement(tag, options);
      if (tag === "a") anchors.push(el as HTMLAnchorElement);
      return el;
    },
  );
});

import { toast } from "sonner";
import { ReviewPatchExport } from "../review-patch-export";

function buildTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "abcdef12-0000-0000-0000-000000000000",
    title: "Task",
    description: "d",
    status: TaskStatus.AWAITING_CEO_APPROVAL,
    team: Team.BACKEND,
    task_type: TaskType.CODE,
    acceptance_criteria: [],
    parent_task_id: null,
    ...overrides,
  } as unknown as Task;
}

function renderExport(task: Task) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ReviewPatchExport task={task} />
    </QueryClientProvider>,
  );
}

describe("ReviewPatchExport", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    lastBlob = null;
    anchors.length = 0;
  });

  it("is hidden on a subtask", () => {
    renderExport(buildTask({ parent_task_id: "parent" }));
    expect(
      screen.queryByRole("button", { name: "Export reviewed patches" }),
    ).toBeNull();
  });

  it("downloads every accepted patch in apply order as one .patch file", async () => {
    tasksApi.getReviewExport.mockResolvedValue({
      root_task_id: "abcdef12",
      patches: [
        {
          artifact_id: "a1",
          task_id: "t1",
          sha256: "s1",
          paths: ["a"],
          patch: "diff --git a/a b/a\n+one",
        },
        {
          artifact_id: "a2",
          task_id: "t2",
          sha256: "s2",
          paths: ["b"],
          patch: "diff --git a/b b/b\n+two\n",
        },
      ],
    });
    renderExport(buildTask());

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Export reviewed patches" }));

    await waitFor(() => expect(lastBlob).not.toBeNull());
    expect(await lastBlob!.text()).toBe(
      "diff --git a/a b/a\n+one\ndiff --git a/b b/b\n+two\n",
    );
    expect(anchors.at(-1)?.download).toBe("reviewed-abcdef12.patch");
  });

  it("reports when nothing has been accepted yet", async () => {
    tasksApi.getReviewExport.mockResolvedValue({
      root_task_id: "abcdef12",
      patches: [],
    });
    renderExport(buildTask());

    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "Export reviewed patches" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Failed to export: No accepted changes to export yet",
      ),
    );
    expect(lastBlob).toBeNull();
  });
});
