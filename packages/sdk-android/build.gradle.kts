plugins {
    kotlin("jvm") version "1.9.24" apply false
    id("com.android.library") version "8.5.2" apply false
    id("com.android.application") version "8.5.2" apply false
    kotlin("android") version "1.9.24" apply false
    `maven-publish`
}

allprojects {
    group = "com.rulemind"
    version = "4.1.0-beta.1"
}
