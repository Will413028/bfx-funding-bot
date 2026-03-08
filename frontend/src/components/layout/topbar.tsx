"use client";

import { Menu } from "lucide-react";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetContent, SheetTrigger } from "@/components/ui/sheet";
import { LocaleSwitcher } from "./locale-switcher";
import { SidebarNav } from "./sidebar-nav";

export function TopBar() {
  const [open, setOpen] = useState(false);

  return (
    <header className="flex h-14 items-center justify-between border-b border-white/5 bg-zinc-950/80 px-4 backdrop-blur-xl md:px-6">
      <div className="flex items-center gap-3">
        {/* Mobile menu */}
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetTrigger asChild>
            <Button variant="ghost" size="icon-sm" className="md:hidden">
              <Menu className="size-5" />
            </Button>
          </SheetTrigger>
          <SheetContent
            side="left"
            className="w-64 border-white/5 bg-zinc-950 p-0"
          >
            <div className="flex h-14 items-center px-6">
              <span className="text-sm font-bold tracking-tight">
                BFX Funding Bot
              </span>
            </div>
            <Separator className="bg-white/5" />
            <SidebarNav onNavigate={() => setOpen(false)} />
          </SheetContent>
        </Sheet>

        {/* Logo (visible on mobile, hidden on desktop since sidebar has it) */}
        <span className="text-sm font-bold tracking-tight md:hidden">
          BFX Funding Bot
        </span>
      </div>

      <LocaleSwitcher />
    </header>
  );
}
