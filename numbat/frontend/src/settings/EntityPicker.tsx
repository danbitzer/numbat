import { Check, ChevronsUpDown } from "lucide-react";
import { useState } from "react";
import type { Entity } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";

interface Props {
  value: string;
  onChange: (entityId: string) => void;
  entities: Entity[]; // empty while loading/unreachable — free text still works
  domains: string[];
  optional?: boolean;
  invalid?: boolean;
}

/** Searchable entity combobox — the headline UX win over the Supervisor
 * options page: pick `sensor.load_power` from a filtered dropdown with
 * friendly names instead of typing entity IDs. Unknown IDs can still be
 * entered as typed (HA may not have the entity yet). */
export function EntityPicker({ value, onChange, entities, domains, optional, invalid }: Props) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");

  const candidates = entities.filter((e) => domains.includes(e.domain));
  const selected = candidates.find((e) => e.entity_id === value);
  const pick = (entityId: string) => {
    onChange(entityId);
    setOpen(false);
    setSearch("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="outline"
          role="combobox"
          aria-expanded={open}
          aria-invalid={invalid || undefined}
          className="h-auto w-full justify-between rounded-md bg-secondary px-[13px] py-[11px] text-[13px] font-normal"
        >
          {value ? (
            // Selected display: friendly name only — the id is visible in the
            // dropdown; raw ids only show for unknown/typed entities.
            <span className="truncate">{selected ? selected.name : value}</span>
          ) : (
            <span className="text-muted-foreground">{optional ? "not used" : "select…"}</span>
          )}
          <ChevronsUpDown className="size-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-(--radix-popover-trigger-width) min-w-72 p-0" align="start">
        <Command>
          <CommandInput
            placeholder={`Search ${domains.join(", ")}…`}
            value={search}
            onValueChange={setSearch}
          />
          <CommandList>
            <CommandEmpty>No matching entities.</CommandEmpty>
            <CommandGroup>
              {search && !candidates.some((e) => e.entity_id === search) && (
                // forceMount: always reachable — cmdk's fuzzy filter would
                // otherwise hide this whenever the search matches anything
                <CommandItem forceMount value="__typed__" onSelect={() => pick(search)}>
                  Use “{search}” as typed
                </CommandItem>
              )}
              {optional && (
                <CommandItem value="__none__" onSelect={() => pick("")}>
                  <span className="text-muted-foreground">(not used)</span>
                </CommandItem>
              )}
              {candidates.map((e) => (
                <CommandItem
                  key={e.entity_id}
                  value={`${e.name} ${e.entity_id}`}
                  onSelect={() => pick(e.entity_id)}
                >
                  <Check
                    className={cn("size-4", e.entity_id === value ? "opacity-100" : "opacity-0")}
                  />
                  <span className="flex min-w-0 flex-col">
                    <span className="truncate">{e.name}</span>
                    <span className="text-muted-foreground truncate text-xs">{e.entity_id}</span>
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
