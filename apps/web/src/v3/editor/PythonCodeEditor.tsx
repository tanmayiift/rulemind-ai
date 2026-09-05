"use client";

import * as React from "react";
import { autocompletion, closeBrackets, closeBracketsKeymap, completionKeymap, type Completion, type CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { defaultKeymap, history, historyKeymap, indentWithTab } from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import { HighlightStyle, indentUnit, syntaxHighlighting } from "@codemirror/language";
import { Annotation, Compartment, EditorState } from "@codemirror/state";
import { EditorView, keymap, lineNumbers } from "@codemirror/view";
import { tags } from "@lezer/highlight";

// Marks a transaction as "this came from props, not from the keyboard", so the update listener
// can tell an external sync from a real edit. A ref flag would work for the synchronous case but
// not for a dispatch that lands while another is in flight; an annotation travels with the
// transaction itself.
const fromProps = Annotation.define<boolean>();

import { useRuleMindStore } from "../../lib/store";
import { useTheme } from "../kit";
import type { ThemeTokens } from "../theme";
import { rulemindCompletions, type RuleMindCompletionScope } from "./rulemind-completion";

// CodeMirror 6, not Monaco, and the reason is the deployment target rather than taste.
// The dashboard ships as a Render STATIC SITE (NEXT_OUTPUT=export, see next.config.js), so
// every byte it runs has to be in the bundle: `@monaco-editor/react` pulls Monaco from
// jsDelivr at run time by default, and self-hosting it under a static export means hand-rolled
// worker plumbing that `output: "export"` does not emit for you. Monaco is also ~5MB against
// CodeMirror's ~250KB, for one editor on one page, in an app that carries nine runtime
// dependencies on purpose. Parity row P13 counts the two as equivalent and asks for
// highlighting plus RuleMind-aware completion, not for Monaco by name.

/** The editing surface itself carries the test id — see the comment on `contentAttributes`. */
export interface PythonCodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  /** Live page state the completion source reads. Re-read on every keystroke, never captured. */
  scope: RuleMindCompletionScope;
  testId?: string;
  ariaLabel?: string;
  height?: number;
}

function highlightStyle(theme: ThemeTokens): HighlightStyle {
  // Every colour is a theme token, so the editor follows the light/dark toggle like the rest
  // of the shell instead of becoming a bright rectangle in dark mode.
  return HighlightStyle.define([
    { tag: [tags.keyword, tags.controlKeyword, tags.moduleKeyword, tags.operatorKeyword], color: theme.purple },
    { tag: tags.definitionKeyword, color: theme.purple, fontWeight: "600" },
    { tag: [tags.string, tags.special(tags.string)], color: theme.success },
    { tag: [tags.number, tags.bool, tags.null, tags.atom], color: theme.warning },
    { tag: [tags.comment, tags.lineComment, tags.blockComment], color: theme.dim, fontStyle: "italic" },
    { tag: [tags.function(tags.variableName), tags.function(tags.definition(tags.variableName))], color: theme.accent },
    { tag: tags.definition(tags.variableName), color: theme.text },
    { tag: [tags.propertyName, tags.attributeName], color: theme.text },
    // The `@variable(...)` decorator, which is the one line every variable must get right.
    { tag: [tags.meta, tags.annotation], color: theme.accent, fontWeight: "600" },
    { tag: [tags.self, tags.className, tags.typeName], color: theme.warning },
    { tag: [tags.standard(tags.variableName), tags.standard(tags.name)], color: theme.purple },
    { tag: [tags.operator, tags.punctuation, tags.bracket, tags.separator], color: theme.muted },
    { tag: tags.variableName, color: theme.codeText },
    { tag: tags.invalid, color: theme.danger },
  ]);
}

function editorTheme(theme: ThemeTokens, dark: boolean) {
  return EditorView.theme(
    {
      "&": {
        height: "100%",
        background: theme.editor,
        color: theme.codeText,
        border: "1px solid " + theme.border,
        borderRadius: "10px",
        fontSize: "var(--rm-fs-code)",
      },
      "&.cm-focused": { outline: "none", borderColor: theme.borderFocus, boxShadow: theme.ring },
      ".cm-scroller": { background: theme.editor, fontFamily: "var(--font-mono)", lineHeight: "1.6", overflow: "auto", borderRadius: "10px" },
      // The e2e theme test reads background-color off this element, because this element is
      // what the user actually types into.
      ".cm-content": { background: theme.editor, caretColor: theme.accent, fontFamily: "var(--font-mono)", padding: "10px 12px" },
      ".cm-gutters": { background: theme.editor, color: theme.dim, border: "none", paddingLeft: "6px" },
      ".cm-activeLine": { background: theme.accentBg },
      ".cm-activeLineGutter": { background: theme.accentBg, color: theme.accent },
      ".cm-cursor, .cm-dropCursor": { borderLeftColor: theme.accent, borderLeftWidth: "2px" },
      "&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection": { background: theme.accentBg },
      ".cm-matchingBracket, .cm-nonmatchingBracket": { background: theme.accentBg, color: theme.accent },
      ".cm-tooltip": {
        background: theme.card,
        border: "1px solid " + theme.border,
        borderRadius: "8px",
        boxShadow: theme.shadowMd,
        color: theme.text,
      },
      ".cm-tooltip.cm-tooltip-autocomplete > ul": { fontFamily: "var(--font-mono)", maxHeight: "16em" },
      ".cm-tooltip.cm-tooltip-autocomplete > ul > li": { padding: "3px 8px" },
      ".cm-tooltip-autocomplete > ul > li[aria-selected]": { background: theme.accentBg, color: theme.accent },
      ".cm-completionDetail": { color: theme.dim, fontStyle: "normal", marginLeft: "10px" },
      ".cm-completionInfo": { background: theme.card, border: "1px solid " + theme.border, borderRadius: "8px", color: theme.muted, padding: "6px 8px" },
      ".cm-completionIcon": { color: theme.dim },
    },
    { dark }
  );
}

/** Turn a `RuleMindCompletionResult` into what CodeMirror wants. */
function toCompletionResult(result: ReturnType<typeof rulemindCompletions>): CompletionResult | null {
  if (!result) {
    return null;
  }
  const options: Completion[] = result.options.map((option) => ({
    label: option.label,
    type: option.kind,
    detail: option.detail,
    info: option.info,
    boost: option.boost,
    // A dot completion has to swallow the dot it replaces (`payload.` -> `payload["summary"]`),
    // so it applies from further left than the popup filters from.
    apply:
      option.insertFrom === result.from
        ? option.insert
        : (view: EditorView, _completion: Completion, _from: number, to: number) => {
            view.dispatch({
              changes: { from: option.insertFrom, to, insert: option.insert },
              selection: { anchor: option.insertFrom + option.insert.length },
            });
          },
  }));
  return { from: result.from, options };
}

export function PythonCodeEditor(props: PythonCodeEditorProps) {
  const theme = useTheme();
  const themeMode = useRuleMindStore((state) => state.themeMode);
  const container = React.useRef<HTMLDivElement | null>(null);
  const view = React.useRef<EditorView | null>(null);
  const themeCompartment = React.useRef(new Compartment());

  // The editor is created once and never re-created, so both of these are read through refs:
  // a captured `props.scope` would freeze the completions at whatever source was selected
  // when the editor mounted, which is exactly the bug the parity row is about.
  const scope = React.useRef(props.scope);
  scope.current = props.scope;
  const onChange = React.useRef(props.onChange);
  onChange.current = props.onChange;

  const testId = props.testId;
  const ariaLabel = props.ariaLabel;

  React.useEffect(() => {
    const parent = container.current;
    if (!parent) {
      return;
    }
    const completionSource = (context: CompletionContext): CompletionResult | null => {
      const line = context.state.doc.lineAt(context.pos);
      return toCompletionResult(
        rulemindCompletions(
          { textBefore: context.state.sliceDoc(line.from, context.pos), pos: context.pos, explicit: context.explicit },
          scope.current
        )
      );
    };

    const instance = new EditorView({
      parent,
      state: EditorState.create({
        doc: props.value,
        extensions: [
          lineNumbers(),
          history(),
          python(),
          indentUnit.of("    "),
          EditorState.tabSize.of(4),
          closeBrackets(),
          autocompletion({ override: [completionSource], icons: false }),
          keymap.of([...closeBracketsKeymap, ...completionKeymap, ...defaultKeymap, ...historyKeymap, indentWithTab]),
          EditorView.lineWrapping,
          // Playwright's `fill()` needs a real editing surface, and CodeMirror's is this
          // contenteditable — so the test id lives on it rather than on a wrapper div. The
          // alternative, a hidden textarea kept in sync, would move the id onto an element
          // nobody types into and would quietly hollow out the theme assertion in
          // e2e/rulemind-v3.spec.ts, which reads the editor's background colour.
          EditorView.contentAttributes.of({
            ...(testId ? { "data-testid": testId } : {}),
            ...(ariaLabel ? { "aria-label": ariaLabel } : {}),
            spellcheck: "false",
            autocapitalize: "off",
          }),
          themeCompartment.current.of([]),
          EditorView.updateListener.of((update) => {
            // `docChanged` alone is not "the user typed". It is also true for the programmatic
            // replace in the value-sync effect below, and that difference is not cosmetic: when
            // the page loads a saved variable it calls setCode(saved.code), the effect pushes
            // that into the document, this listener reported it as an edit, and VariablesPage's
            // onChange ran beginDraft() -- which sets draftMode and clears selectedId. Selecting
            // a saved variable therefore deselected it, and `variable-promote` never rendered.
            // Caught by e2e/rulemind-v3.spec.ts:143, which is the only reason it was not shipped.
            if (update.docChanged && !update.transactions.some((tr) => tr.annotation(fromProps))) {
              onChange.current(update.state.doc.toString());
            }
          }),
        ],
      }),
    });
    view.current = instance;
    return () => {
      instance.destroy();
      view.current = null;
    };
    // Mount-only: `value` and `scope` are pushed in by the effects below, not by remounting.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ariaLabel, testId]);

  React.useEffect(() => {
    view.current?.dispatch({
      effects: themeCompartment.current.reconfigure([editorTheme(theme, themeMode === "dark"), syntaxHighlighting(highlightStyle(theme))]),
    });
  }, [theme, themeMode]);

  React.useEffect(() => {
    const instance = view.current;
    // Only a change that did NOT come from the editor is worth pushing back in; without the
    // guard every keystroke would round-trip through a full document replace and lose the
    // cursor.
    if (instance && props.value !== instance.state.doc.toString()) {
      instance.dispatch({
        changes: { from: 0, to: instance.state.doc.length, insert: props.value },
        annotations: fromProps.of(true),
      });
    }
  }, [props.value]);

  return (
    <div
      ref={container}
      style={{
        width: "100%",
        height: props.height ?? 420,
        minHeight: 160,
        // Matches the `resize: vertical` the textarea this replaced offered.
        resize: "vertical",
        overflow: "hidden",
      }}
    />
  );
}
