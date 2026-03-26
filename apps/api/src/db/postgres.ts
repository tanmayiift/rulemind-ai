import { Client } from "pg";
import { emptyState, StateStore, type PlatformState } from "./adapter";

export class PostgresPlatformStore extends StateStore {
  private readonly client: Client;

  constructor(databaseUrl: string) {
    super();
    this.client = new Client({
      connectionString: databaseUrl
    });
  }

  private async ensureConnection() {
    if (!(this.client as unknown as { _connected?: boolean })._connected) {
      await this.client.connect();
      (this.client as unknown as { _connected?: boolean })._connected = true;
    }
  }

  protected async loadState(): Promise<PlatformState> {
    await this.ensureConnection();
    await this.migrate();
    const result = await this.client.query<{ payload: PlatformState }>(
      "select payload from platform_state where id = $1",
      ["default"]
    );
    return result.rows[0]?.payload ?? emptyState();
  }

  protected async saveState(state: PlatformState): Promise<void> {
    await this.ensureConnection();
    await this.client.query(
      `insert into platform_state (id, payload, updated_at)
       values ($1, $2::jsonb, now())
       on conflict (id)
       do update set payload = excluded.payload, updated_at = now()`,
      ["default", JSON.stringify(state)]
    );
  }

  override async migrate() {
    await this.ensureConnection();
    await this.client.query(
      `create table if not exists platform_state (
        id text primary key,
        payload jsonb not null,
        updated_at timestamptz not null default now()
      )`
    );
  }

  override async ping(): Promise<boolean> {
    await this.ensureConnection();
    const result = await this.client.query("select 1");
    return result.rowCount === 1;
  }
}
