import { ChatPanel } from "@/components/chat-panel";
import { Shell } from "@/components/glass";

export const metadata = { title: "Tutor" };

export default function ChatPage() {
  return (
    <Shell title="Ask the books" wide>
      <p className="mb-6 max-w-2xl text-sm leading-relaxed text-parchment-dim">
        Answers come from the shared library and your own uploads, with the page they
        came from attached. If the material does not cover something, it says so
        rather than guessing.
      </p>
      <ChatPanel />
    </Shell>
  );
}
