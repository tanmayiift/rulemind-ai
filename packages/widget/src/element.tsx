import { createRoot, type Root } from "react-dom/client";
import { RuleBuilder } from "./RuleBuilder";

class RuleMindWidgetElement extends HTMLElement {
  private root: Root | null = null;

  connectedCallback() {
    if (!this.root) {
      this.root = createRoot(this);
    }

    this.render();
  }

  static get observedAttributes() {
    return ["environment", "read-only"];
  }

  attributeChangedCallback() {
    this.render();
  }

  private render() {
    this.root?.render(
      <RuleBuilder
        environment={(this.getAttribute("environment") as "dev" | "staging" | "prod" | null) ?? "dev"}
        readOnly={this.getAttribute("read-only") === "true"}
      />
    );
  }
}

export function defineRuleMindWidget(tagName = "rulemind-widget") {
  if (!customElements.get(tagName)) {
    customElements.define(tagName, RuleMindWidgetElement);
  }
}
