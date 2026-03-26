import type { AppConfig } from "../config";
import type { PlatformStore } from "./adapter";
import { FilePlatformStore } from "./file";
import { MongoPlatformStore } from "./mongodb";
import { PostgresPlatformStore } from "./postgres";
import { SqlitePlatformStore } from "./sqlite";

export function createStore(config: AppConfig): PlatformStore {
  switch (config.databaseAdapter) {
    case "postgres":
      return new PostgresPlatformStore(config.databaseUrl);
    case "sqlite":
      return new SqlitePlatformStore(config.sqlitePath);
    case "mongodb":
      return new MongoPlatformStore(config.mongodbUrl);
    case "file":
    default:
      return new FilePlatformStore(config.fileStorePath);
  }
}
