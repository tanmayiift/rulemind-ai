import { emptyState, StateStore, type PlatformState } from "./adapter";

export class SqlitePlatformStore extends StateStore {
  private database: any;

  constructor(private readonly sqlitePath: string) {
    super();
  }

  private async getDatabase() {
    if (!this.database) {
      const module = await import("better-sqlite3");
      const Database = module.default;
      this.database = new Database(this.sqlitePath);
      await this.migrate();
    }

    return this.database;
  }

  protected async loadState(): Promise<PlatformState> {
    const database = await this.getDatabase();
    const row = database.prepare("select payload from platform_state where id = ?").get("default") as { payload?: string } | undefined;
    return row?.payload ? (JSON.parse(row.payload) as PlatformState) : emptyState();
  }

  protected async saveState(state: PlatformState): Promise<void> {
    const database = await this.getDatabase();
    database
      .prepare(
        `insert into platform_state (id, payload, updated_at)
         values (?, ?, datetime('now'))
         on conflict(id)
         do update set payload = excluded.payload, updated_at = datetime('now')`
      )
      .run("default", JSON.stringify(state));
  }

  override async migrate() {
    const database = this.database ?? (await this.getDatabase());
    database.exec(
      `create table if not exists platform_state (
        id text primary key,
        payload text not null,
        updated_at text not null default (datetime('now'))
      )`
    );
  }

  override async ping(): Promise<boolean> {
    const database = await this.getDatabase();
    const row = database.prepare("select 1 as ok").get() as { ok: number };
    return row.ok === 1;
  }
}
