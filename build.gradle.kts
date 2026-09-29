// Top-level build file where you can add configuration options common to all sub-projects/modules.
plugins {
    alias(libs.plugins.android.application) apply false
    alias(libs.plugins.kotlin.android) apply false
    alias(libs.plugins.ksp) apply false
    alias(libs.plugins.kotlin.serialization) apply false
    alias(libs.plugins.secrets) apply false

    // ktlint est l'UNIQUE gate de lint. Il est déclaré ici puis appliqué dans
    // `:app` (il lui faut les source sets du module Android, qui portent toutes
    // les sources Kotlin du dépôt). La tâche bloquante est `:app:ktlintCheck` :
    // voir le bloc `ktlint { … }` de app/build.gradle.kts et `.editorconfig`.
    alias(libs.plugins.ktlint) apply false
}
