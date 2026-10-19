const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const sandbox = {};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,"../platform_discovery/assets/personal.js"),"utf8"),sandbox);
const grid=sandbox.MovieClawPersonal.grid;
for(const [width,height,top,columns] of [
  [320,568,360,3],[390,844,410,3],[844,390,260,6],
  [768,1024,380,6],[1080,800,360,7],[1440,900,350,9],
  [2320,1210,377,11],[3840,2160,377,11],[800,3000,300,6],
]) {
  const layout=grid({width,height,top,columns});
  assert.equal(layout.count%columns,0,"every row must be complete");
  assert.equal(layout.count,layout.rows*columns);
  assert.ok(layout.count>=3&&layout.count<=96);
  assert.ok(layout.height>=1);
  if(width>767)assert.ok(top+layout.height<=height+1,"desktop wall must fit below its final heading");
  else assert.ok(layout.height>=Math.min(height*.65,width/columns*3)-1,"phone retains two useful poster rows even below long controls");
}
const desktop=grid({width:2320,height:1210,top:377,columns:11});
const phone=grid({width:390,height:844,top:410,columns:3});
assert.equal(desktop.count,33);
assert.equal(phone.count,6);
assert.notEqual(desktop.count,48,"the wall must adapt rather than retain a fixed batch size");
for(const [width,height,columns] of [[320,568,3],[844,390,6],[1440,900,9],[3840,2160,11]]) {
  const layout=grid({width,height,top:0,columns});
  assert.equal(layout.height,height,"immersive layout must fill the full viewport");
  assert.equal(layout.count%columns,0,"immersive layout cannot leave a partial row");
  assert.ok(layout.count>=3&&layout.count<=96);
}
console.log("PASS: 9 regular + 4 fullscreen viewports, complete rows, 3–96 bounds, viewport bottom fit, desktop/phone adaptation");
// Mosaic columns overscan the outer edge rather than stretching every poster.
for(const [width,height,top,columns] of [[358,844,444,2],[1203,720,450,9],[1504,1210,450,11],[844,390,450,6],[1440,900,0,9],[358,844,0,2]]){
 const layout=grid({width,height,top,columns,mosaic:true});
 assert.equal(layout.count,layout.rows*layout.columns);
 assert.ok(layout.count<=96);
 assert.ok(layout.rows*(width/layout.columns*1.5)>=layout.height+7,'natural poster columns cover both floating outer edges');
 assert.ok(layout.height>1,'short landscape screens retain a usable, scrollable poster field');
}
const fitted=grid({width:1203,height:720,top:450,columns:9,mosaic:true});
assert.ok(fitted.columns<9,'short desktop wall uses larger complete posters instead of a mostly clipped second row');
const items=Array.from({length:18},(_,i)=>({id:i})),groups=sandbox.MovieClawPersonal.columnize(items,6);
assert.equal(groups.length,6);assert.ok(groups.every(group=>group.length===3));
for(let row=0;row<3;row++)for(let col=0;col<6;col++)assert.equal(groups[col][row],items[row*6+col],'column tracks retain row-major visual order and item identity');
assert.equal(new Set(groups.flat()).size,items.length,'no duplicate posters are introduced for animation');
console.log('PASS: natural-ratio mosaic coverage, short-screen fallback, adaptive clipping and duplicate-free column tracks');
const preferred=sandbox.MovieClawPersonal.preferredColumns;
for(const width of [1079,1354,2224,3744]){
 const columns=preferred({width}),tile=(width-13*(columns-1))/columns;
 assert.ok(tile>=220,'wide-screen card posters stay large and readable');
 assert.ok(tile<300,'ordinary screens adapt column count without one oversized poster');
 const mosaic=grid({width,height:1210,top:450,columns,mosaic:true});
 assert.ok(width/mosaic.columns>=200,'mosaic fitting must not make the posters tiny again');
}
assert.equal(preferred({width:358,viewportWidth:390}),2,'phone keeps two touch-friendly columns');
const giant=grid({width:7200,height:2160,top:0,columns:24,mosaic:true});
assert.equal(giant.height,2160,'extreme fullscreen remains covered within the batch limit');
assert.ok(giant.count<=96);
console.log('PASS: full-width large posters, two-column phone layout and extreme fullscreen coverage');

for(const [width,height] of [[320,568],[390,844],[844,390],[1280,720],[1440,900],[1920,1080],[2560,1440],[3440,1440],[3840,2160],[7200,2160],[800,3000]]){
 const fitted=grid({width,height,columns:8,top:0,mosaic:true,fitted:true});
 assert.ok(fitted.count>=3&&fitted.count<=96);
 assert.ok(fitted.columns*fitted.tileWidth<=width+.001,"all complete columns fit the viewport");
 assert.ok(fitted.rows*fitted.tileWidth*1.5<=height+.001,"last row is never clipped");
 assert.ok(fitted.tileWidth>=Math.min(width/3,175),"fitting cannot turn the wall into miniature posters");
}
console.log('PASS: 11 complete fullscreen ratios preserve 2:3 posters without clipping the last row');
