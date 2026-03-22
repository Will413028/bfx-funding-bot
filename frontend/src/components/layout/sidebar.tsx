import { Separator } from "@/components/ui/separator";
import { SidebarNav } from "./sidebar-nav";

export function Sidebar() {
  return (
    <aside
      aria-label="Navigation sidebar"
      className="hidden w-64 flex-col border-r border-white/5 bg-zinc-950 md:flex"
    >
      <div className="flex h-14 items-center px-6">
        <span className="text-sm font-bold tracking-tight">
          BFX Funding Bot
        </span>
      </div>
      <Separator className="bg-white/5" />
      <SidebarNav />
    </aside>
  );
}
