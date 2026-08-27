import { BookList } from "@/components/book-list";
import { Shell } from "@/components/glass";

export const metadata = { title: "Books" };

export default function BooksPage() {
  return (
    <Shell title="Books">
      <p className="mb-6 max-w-2xl text-sm leading-relaxed text-parchment-dim">
        Everything you upload is private to you — nobody else can read it, and nobody
        else can write a test from it. You can write your own papers from it either
        way. Share a book and it joins the library everyone signed in can read and
        draw papers from.
      </p>
      <BookList />
    </Shell>
  );
}
