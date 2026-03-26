import { MongoClient } from "mongodb";
import { emptyState, StateStore, type PlatformState } from "./adapter";

export class MongoPlatformStore extends StateStore {
  private readonly client: MongoClient;

  constructor(private readonly mongodbUrl: string) {
    super();
    this.client = new MongoClient(mongodbUrl);
  }

  private async collection() {
    if (!(this.client as unknown as { _connected?: boolean })._connected) {
      await this.client.connect();
      (this.client as unknown as { _connected?: boolean })._connected = true;
    }

    return this.client.db().collection<{ _id: string; payload: PlatformState }>("platform_state");
  }

  protected async loadState(): Promise<PlatformState> {
    const collection = await this.collection();
    const document = await collection.findOne({ _id: "default" });
    return document?.payload ?? emptyState();
  }

  protected async saveState(state: PlatformState): Promise<void> {
    const collection = await this.collection();
    await collection.updateOne({ _id: "default" }, { $set: { payload: state } }, { upsert: true });
  }

  override async migrate() {
    await this.collection();
  }

  override async ping(): Promise<boolean> {
    const admin = this.client.db().admin();
    const response = await admin.ping();
    return response.ok === 1;
  }
}
