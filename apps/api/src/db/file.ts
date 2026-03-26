import { mkdir, readFile, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { emptyState, StateStore, type PlatformState } from "./adapter";

export class FilePlatformStore extends StateStore {
  private readonly filePath: string;

  constructor(rootPath: string) {
    super();
    this.filePath = join(rootPath, "platform-state.json");
  }

  protected async loadState(): Promise<PlatformState> {
    try {
      const raw = await readFile(this.filePath, "utf8");
      return JSON.parse(raw) as PlatformState;
    } catch {
      return emptyState();
    }
  }

  protected async saveState(state: PlatformState): Promise<void> {
    await mkdir(dirname(this.filePath), { recursive: true });
    await writeFile(this.filePath, JSON.stringify(state, null, 2), "utf8");
  }
}
