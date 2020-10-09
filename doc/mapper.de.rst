=================
Der Mudlet Mapper
=================

Dieses Paket enthält einen "echten" Kartengenerator und ein Haufen
Funktionen, um ein MUD auszukundschaften.

Einrichtung
===========

Du brauchst "Mudlet", einen Character in einem von MudPyC unterstützten MUD
das du schon ein bisschen kennst, und ein wenig Geduld um deinen Fingern
ein paar neue Makros beizubringen.

Im Idealfall solltest du die Mudlet-Version von Smurf verwenden, weil du
damit die Raumbezeichnungen anzeigen lassen kannst.

Du brauchst außerdem eine Datenbank. Datenbanken sind nett, weil man sie
nicht sichern muss, ganz im Gegensatz zur Mudlet-Karte. Somit legen wir
eine an. Dieses Beispiel verwendet sqlite: kopiere einfach
``mapper.cfg.sample`` nach ``mapper.cfg``.

Die unterstützten MUDs findest du im Verzeichnis ``mudpyc/driver/XX``.
``XX`` ist "de" oder "en" je nach MUD-Sprache. MudPyC auf ein weiteres
MUD anzupassen ist nicht allzu schwer; bitte schick uns den Code, damit
andere Spieler auch was davon haben und wir ihn am Laufen halten können,
wenn wir was umstrukturieren müssen.

Als Nächtes starte ``./run -c mapper.cfg --migrate``, das legt die
Datenbank an. Dann starte Mudlet, lege für deinen Character ein neues
Profil (ohne irgendwelche Makropakete) an, und ggf. gehe in ein Gebiet das
du kennst und in dem es brauchbare Kompassrichtungen gibt, d.h. nach
``norden`` und ``sueden`` bist du wieder da, wo du hergekommen bist.

Nun lade das Modul ``mudpyc.xml`` und tippe ``#py+`` (und die Eingabetaste,
versteht sich).

Du solltest die Meldung ``Verbunden.`` sehen, die dir sagt dass Mudlet und
MudPyc miteinander reden. Mudlet wird nun von MudPyC fernbedient (oder
umgekehrt).

Jetzt fangen wir das Mappen an, Wenn du noch keine Karte hast, dann laufe
einfach ein bisschen herum. Die Kartenansicht in Mudlet sollte automatisch
eine brauchbare Karte bauen.

Wenn du schon eine Karte hast, dann ist die Sache nicht ganz so einfach.
Lade deine Karte und tippe ``lua getRoomHashByID(123)`` ("123" ist die
Nummer des Raums, in dem du gerade bist). Du solltest eine hexadezimale
Zahl sehen. Das bedeutet, dass dein MUD dir via GMCP die IDs der Räume
geschickt hat, in denen du bist, und dass dein bisheriges Mappingprogramm
diese IDs in die Karte eingetragen hat.

Wenn dieser "lua"-Befehl nichts ausgegeben hat, dann kann das alles
trotzdem funktionieren, aber das Ganze ist ein bisschen komplizierter. Wie
MudPyC mit Räumen ohne GMCP-ID zurechtkommt, beschreibe ich weiter unten
(TODO). 

OK, dein altes Mappingskript ist nicht aktiv, wir müssen also als Erstes
deinen aktuellen Ort finden. Klicke in der Karte auf den Raum, in dem du
bist, und tippe ``#v ?``. Das sollte eine Fehlermeldung produzieren, weil
MudPyC den Raum mit dieser Nummer noch nicht kennt. Das ist OK: mit ``#ms
??`` sagst du MudPyC, dass du im angeklickten Raum bist und dass es diesen
in seine Datenbank übernehmen soll.

Der nächste Schritt ist ``#mdi .``. Damit schaut sich MudPyC den aktuellen
Raum an, oder genauer: dessen Ausgänge. Dann importiert es die Räume, zu
denen diese Ausgänge zeigen, und macht mit denen dasselbe. 

Das funktioniert natürlich nicht mit Räumen, für die es nur einen Rückweg
gibt. Wenn du solche in der Karte hast, dann klicke auf jeden von ihnen und
sage ``#mdi ??``.

Gratuliere, du hast jetzt deine Mudlet-Karte in die MudPyC-Datenbank kopiert.
Während du spielst, updatet MudPyC beide parallel.

Wenn du die Datenbank zu Mudlet zurückkopieren willst, weil du vergessen
hast, die Mudlet-Karte zu speichern (oder es wegen eines Absturzes nicht
konntest), verwende ``#mds``. Umgekehrt, wenn du die Karte später mit
deinem alten Mappingskript weiterverwendet hast und die Datenbank updaten
willst, dann geht das mit ``#mdt``.

Wenn du Beides gemacht hast, dann hast du allerdings ggf. ein Problem, weil
neue Mudlet-Raumnummern jetzt auf andere Räume zeigen als die Nummern in
MudPyC. Um das zu beheben, verwende ``#mdi! ?``. Das löscht die
Mudlet-Raumnummern aus der Datenbank und baut dann die Zuordnung
zwischen beiden neu auf. Du musst dafür einen Raum auswählen, den es
schon gab, bevor sich die Versionen auseinanderentwickelt haben.

Raumnamen
=========

Dein MUD sendet kurze Raumnamen. Die Mudlet-Version von Smurf kann diese
anzeigen, wenn du das einschaltest.

Die Namen erscheinen normalerweise zentriert unter dem Raum. Die Karte
sieht (nach Meinung des Autors) am besten aus, wenn du die Raumgröße auf 10
setzst und die Räume 5 Einheiten auseinander sind.

Die Namen werden wahrscheinlich teilweise übereinanderstehen. Du kannst die
Namen der ausgewählten Räume mit Strg-links/rechts horizontal verschieben;
wenn du zusätzlich die Großschreibtaste drückst, werden sie vertikal
verschoben. Um wieviel jeder Tastendruck die Namen schiebt, kannst du
einstellen. Mit ``#rs`` kannst du den Namen eines Raums manuell ändern,
wenn er nicht eindeutig genug oder zu lang ist.

Die Mudlet-Version von Smurf verwendet das experimentelle Kartenformat 21,
um die Positionen der Namen zu speichert. Wenn du die nicht verwenden
kannst oder willst, dann stellt ``#mds`` nach einem Neustart von Mudlet den
alten Zustand wieder her.


Diese seltsamen ``#``-Makros
============================

MudPyC verwnendet absichtlich keine ganzen Wörter als Befehle. Das macht
das MUD bereits. Außerdem ist ``#auto finde naechste kneipe`` auf Dauer
nervig zu tippen.

Außerdem wollen wir, dass die Makros ein Inhaltsverzeichnis und einen
Hilfstext haben. Du kannst an alle Makros ein Fragezeichen anhängen: dann
wird ein mehrzeiliger Hilfstext für diesen Befehl angezeigt (wenn es ihn
gibt), plus eine Zeile für jedes Unter-Makro das mit dieser Folge anfängt.

``#?`` zeigt somit das übergeordnete Inhaltsverzeichnis für alle Makros an.
Sie sind nach Funktion gruppiert und hoffentlich ist das alles einigermaßen
selbsterklärend.

Viele Makros haben Argumente. Manche sind Raumnummern oder Einzelwörter.
Auch Ausgänge sind Einzelwörter. Zusammengesetzte Wörter wie "betrete haus"
kannst du mit Anführungszeichen schreiben oder mit ``\\`` vor den
Leerstellen als ein Wort kennzeichnen.


Raumauswahl
-----------

Du kannst Räume angeben mit …

* positive Zahl: Die Raumummer, die Mudlet anzeigt.
* negative Zahl: ein Raum in der MudPyC-Datenbank. Manche Befehle zeigen
  diese Nummern an, wenn MudPyC die dazugehörende Mudlet-Nummer nicht
  kennt.
* ``.`` – der Raum, in dem sich dein Avatar gerade befindet, bzw. der Raum
  von dem MudPyC das glaubt.
* ``:`` – der Raum, den du gerade verlassen hast.
* ``!`` – der Raum, der in Mudlet als aktueller Ort angezeigt wird.
  Normalerweise ist das auch der Raum, in dem dein Avatar ist, aber mit den
  ``#v``-Makros kannst du ihn ändern.
* ``?`` – der aktuell auf der Mudlet-Karte ausgewählte Raum bzw. der
  Mittelpunkt der Auswahl.
* wenn du gerade eine Wegliste angelegt hast, kannst du die Ziele dieser
  Liste mit ``#N`` (``N`` ist das Ziel des N-ten Wegs) auswählen.

Bewegen
=======

In einer idealen Welt bewegt sich dein Avatar im MUD, und deine Karte zeigt
dazu synchron dessen Ort an.

Wir leben nicht in einer idealen Welt.

Es gibt atypische Ausgänge ("hinten", "suedostvorne", "links"), Räume ohne
GMCP-Info (Labyrinthe), geschlossene Türen, Dunkelheit, und was weiß ich
noch alles.

MudPyC versucht, das alles zu berücksichtigen, aber manchmal rät es falsch.
Im Folgenden ist beschrieben, wie es rät.

* Wenn dein MUD bei Bewegung ein GMCP-Rauminfo schickt, dann legt Mudlet
  einen Raum mit der ID an (wenn es ihn noch nicht gibt), legt einen
  Ausgang zwischen dem alten und neuen Raum an, und bewegt dich da hin.

* Wenn das passiert, ohne dass du einen Befehl eingegeben hast, dann wird
  ein "zeitgesteuerter" Ausgang angelegt.

* Wenn der Ausgang bereits existiert, aber woanders hinzeigt, dann sagt
  MudPyC dir das, lässt aber den "falschen" Ausgang in Ruhe.

* Eine leere GMCP-ID oder eine "Es gibt X sichtbare Ausgänge"-Zeile
  erzwingt einen Raumwechsel. Dasselbe passiert, wenn du einen Befehl
  verwendest, zu dem ein Ausgang gehört, dessen Ziel keine GMCP-ID hat.

* Wenn du im falschen Raum landest, dann sage ``#mn raum`` (das setzt den
  Ausgang und löscht den neuen Raum, wenn gerade einer angelegt wurde),
  oder ``#ms`` (das tut das nicht).

* Falls MudPyC nicht bemerkt hat, dass du dich bewegt hast, verwende
  ``#mm``. Das passiert meistens, wenn du in Dunkelheit reinrennst.
  "Finsternis." automatisch zu erkennen steht auf der TODO-Liste.

* Wenn du einen Befehl eingibst, der genauso lautet wie ein Ausgang, dann
  wird MudPyC diesen Befehl verschlucken und stattdessen die Befehle
  senden, die zu diesem Ausgang gehören. Im Normalfall ändert sich dadurch
  gar nichts, aber du kannst statt "westen" auch "oeffne tuer | schleiche
  nach westen | schliesse tuer" senden lassen.

* "w" wird wie "westen" behandelt. Entsprechendes gilt für nw n no o so s sw
  w ob u.

Mapping
=======

Wenn es zu einer Richtung einen Rückweg gibt und der Zielraum einen Ausgang
in dieser Richtung hat, dann wird er angelegt. Wenn das nicht automatisch
passiert, kannst du es mit ``#mp . rueckrichtung :`` manuell tun. TODO:
Wenn es für die Richtung keinen Rckweg gibt, aber das Ziel einen nicht
besetzten "raus"-Ausgang hat, dann wird der verwendet.

Das Anlegen eines Rückwegs kannst du mit ``#cfr`` abstellen.

Wie neue Räume auf der Karte platziert werden, lässt sich einstellen.
``#c?`` zeigt dir an, welche Parameter du einstellen kannst. Die
Einstellungen werden in der Datenbank gespeichert.

Wenn du die Namen von Ausgänge verkürzen oder mit bekannten Richtungen 
verwenden oder alternative Befehle einstellen willst ("unten" = "binde seil
an baum | klettere seil runter"), dann geht das mit ``#xc`` oder ``#xt``.
Du kannst auch ganze Regionen mit Spezial-Ein- und Ausgangstexten bestücken
(wie zB das Anzünden / Löschen deiner Fackel), mehr dazu unter ``#xf?``.

Wegskripte
==========

Um schnell von A nach B zu gehen, sag ``#g# B`` (angenommen du bist in A).
MudPyC berechnet den schnellsten Weg und lässt dich da hingehen.

Du kannst mit ``#xp`` einen Ausgang "teurer" machen, zB wenn der Aufzug
langsamer ist als die Treppe. Der Preis eines Raums wird mit ``#rp``
eingestellt und ist dasselbe wie das Minimum des Preises aller seiner
Ausgänge (nicht der Eingänge).

Du kannst Räumen via ``#rt`` ein Kürzel zuordnen("Laden", "Kneipe"),
dann eine Liste der drei nächstgelegenen Räume mit diesem Kürzel
erzeugen (``#gt Kneipe``), und dann mit ``#gg`` zu einem dieser Räume
rennen. Das Hinrennen funktioniert nur, wenn du in dem Raum bist, in dem du
das ``#gt`` gemacht hast.

Zur Kneipe zu rennen ist das Eine, aber du willst ja auch wieder zurück.
Dafür gibt es ``#gr``, das zeigt die letzten 9 Räume an vono denen aus du
losgerannt bist. Mit ``#gr N`` gehst du zum N-ten Raum in dieser Liste
zurück.

Du kannst Quests anlegen. Aktuell ist das nur eine Liste von Räumen und
dort auszuführenden Befehlen. Mit ``#qq+ NAME`` kannst du eine anlegen, 
mit ``#q+ BEFEHL`` einen befehl hinzufügen, mit ``#qqa NAME`` die Quest
laden und mit ``#qn`` den jeweils nächsten Befehl ausführen. Mit "#qn" im
Eingabepuffer von Mudlet muss du dafür nur auf die Eingabetaste drücken.
Das auch noch zu automatisieren ist auf der TODO-Liste.

Zum Erkunden gibt es ``#mux``, das dir die nächstgelegenen Räume mit
Ausgängen anzeigt, die du noch nicht erforscht hast. In der Liste sind
keine Räume, die du durch andere solche Räume erreichen würdest; wenn die
mit dabei sein sollen, verwende ``#muxx``. Zum Überspringen von
"uninteressanten" Räumen gibt es Skiplisten, siehe ``#gs?``.

Allgemein überspringen die meisten Automatikbefehle in MudPyC Räume, die
nur über andere Räume erreichbar sind, weil das meistens uninteressant ist.
Wenn du zum Eingang eines Bereichs willst, dann interessiert dich der Weg
zu Eingang B nur dann, wenn er **nicht** über Eingang A führt.

Ansichtsmodus
=============

Der aktuelle Ort in Mudlet ist nicht zwingend derselbe wie in MudPyC. Du
kannst deinen Fokus mit ``#v`` auf jeden Raum der Mudlet-Karte setzen und
mit ``#vg`` beliebig bewegen. Mit ``#v .`` zeigst du wieder den Raum an, in
dem du wirklich bist; mit ``#g# !`` läuft dein Avatar zum angezeigten Raum.


