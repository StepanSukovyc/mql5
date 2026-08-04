Tohle je velmi zajímavý problém a podle toho, co popisuješ, mám pocit, že ve skutečnosti nehledáš klasický Stop Loss, ale spíš mechanismus řízení rizika a stárnutí pozic.

A upřímně, v mnoha strategiích bývá pevný Stop Loss skutečně problematický:

je vidět brokerovi/burze (pokud je na serveru),
může tě vyhodit lokálním šumem,
často zavírá pozici těsně před otočením trhu.

Na druhou stranu strategie bez jakéhokoliv mechanismu ukončování ztrát má tendenci dříve nebo později skončit sérií pozic, které blokují kapitál týdny nebo měsíce.

Co se mi nezdá na návrhu "3 nejméně prodělečné pozice denně"

Psychologicky zní dobře, ale matematicky může být zrádný.

Příklad:

máš pozice:
-5 €
-8 €
-12 €
-20 €
-100 €

Automat uzavře:

-5 €
-8 €
-12 €

a nechá:

-20 €
-100 €

Tedy pravidelně realizuješ malé ztráty a největší průšvihy necháváš růst.

To je často opačně, než by člověk chtěl.

Varianta 1: Time Stop (časový stop)

Tohle je asi nejblíž tomu, co popisuješ.

Pravidlo:

Pokud je pozice ve ztrátě déle než X hodin nebo dnů, zavři ji.

Například:

pozice starší než 48 hodin
a současně je ve ztrátě

=> zavřít.

Výhoda:

nezajímá tě velikost ztráty
pozice nemůže viset věčně
není nikde uložený Stop Loss

To používá hodně systematických traderů.

Varianta 2: Momentum Exit

Tohle se mi líbí ještě víc.

Místo pevného SL:

Long pozice:

otevřeno LONG
cena klesá
pokud trend stále klesá, nic nedělej
pokud vznikne další impuls dolů (nové low, potvrzený downtrend)

=> zavři pozici.

Tedy:

neukončuješ ztrátu podle ceny, ale podle změny struktury trhu.

To je blíž tvému popisu sinusoidy.

Varianta 3: Kapitálová očista

Jednou denně:

spočítat všechny otevřené ztráty
pokud překročí určitou hodnotu

např.

součet floating ztrát > 5 % účtu

pak:

uzavřít nejhorší pozice,
dokud floating ztráta neklesne pod limit.

To chrání účet jako celek.

Varianta 4: Hybrid (moje oblíbená)

Každá pozice má:

Mladá pozice

0-24 h

nech být
Střední

24-72 h

sleduj trend
Stará

72+ h

Pokud:

je stále ve ztrátě
není blízko break-even

=> zavřít.

Tím vlastně říkáš:

"Dal jsem trhu dost času, aby měl pravdu. Neměl ji."

Varianta 5: Průběžné financování ziskem

Velmi zajímavá metoda.

Řekněme že dnes vyděláš:

+100 €

Nevýhodné pozice:

-30 €
-25 €
-20 €

Ziskový den automaticky použije část profitu:

uzavře nejstarší ztrátovou pozici.

Tím se účet postupně "čistí" ze zisků.

Mně osobně se tahle myšlenka pro automatické systémy líbí víc než klasický Stop Loss.

Co bych zkusil já

Pokud je tvoje strategie založená na návratu ceny (mean reversion, sinusový pohyb trhu), šel bych do kombinace:

Žádný veřejný Stop Loss.
Interní emergency stop (např. -3 % až -5 % účtu na pozici, pouze uvnitř automatu).
Time stop:
zavři ztrátové pozice starší než 48-72 hodin.
Denní očista:
uzavři 1-2 nejstarší ztrátové pozice, pokud je účet ten den v realizovaném zisku.

Takový přístup většinou vede k mnohem stabilnější equity křivce než čisté "čekání na návrat", protože řeší hlavní problém těchto strategií: některé obchody se už prostě nevrátí.